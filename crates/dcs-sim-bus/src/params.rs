//! The `sim-bus` device-parameter contract: the addressing and register
//! map a model device declares, parsed identically by the driver-side
//! factory (in `dcs-assembly`) and by the `dcs-sim-bus-device` server
//! binary, so both ends of a rig read the same declaration.
//!
//! A `sim-bus` device's `parameters` carry:
//!
//! - `"address"` (required string): the device server's `host:port` —
//!   where the [`BusDriver`](crate::BusDriver) connects and the default
//!   `--listen` for the server binary;
//! - `"timeout_ms"` (optional non-negative integral number): the
//!   per-request timeout in milliseconds, defaulting to
//!   [`BusDriver::DEFAULT_TIMEOUT`](crate::BusDriver::DEFAULT_TIMEOUT);
//! - `"registers"` (required object): channel name → register
//!   declaration, covering every channel the device declares and naming
//!   no others. An entry is either the register address as a bare
//!   non-negative integer (`"level-raw": 4`) or an object
//!   `{"register": <u16>, "initial": <value>}` whose optional `initial`
//!   seeds the server's register — the driver's neutral value when
//!   absent — and whose variant must match the channel's declared
//!   `value_type`.

use crate::client::BusDriver;
use dcs_core::{Value, ValueKind};
use std::collections::{BTreeMap, HashMap};
use std::time::Duration;

/// The device-kind string the `sim-bus` integration serves — registered
/// in `dcs-assembly`'s `DriverRegistry::standard` as `SIM_BUS_KIND` and
/// matched by the `dcs-sim-bus-device` binary when it selects a device
/// from a model.
pub const DEVICE_KIND: &str = "sim-bus";

/// One channel's register declaration from a `sim-bus` device's
/// `"registers"` parameter.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ChannelRegister {
    /// The register address backing the channel.
    pub register: u16,
    /// The register's power-on value on the device server; `None` seeds
    /// the neutral value of the channel's declared kind.
    pub initial: Option<Value>,
}

/// A `sim-bus` device's parsed `parameters`.
///
/// [`parse`](DeviceParameters::parse) validates the whole block — the
/// address, the timeout, and a register map covering exactly the
/// declared channel set — so a malformed declaration is one named
/// failure at assembly or server start, never a mid-scan surprise.
#[derive(Debug, Clone, PartialEq)]
pub struct DeviceParameters {
    /// The device server's `host:port`, as declared.
    pub address: String,
    /// The per-request timeout for driver exchanges.
    pub timeout: Duration,
    /// Channel name → register declaration, covering every declared
    /// channel exactly once.
    pub registers: BTreeMap<String, ChannelRegister>,
}

impl DeviceParameters {
    /// Parses and validates a `sim-bus` device's `parameters` against
    /// its declared `channels` (name → `value_type`).
    ///
    /// The `Err` text is the diagnostic detail the caller wraps —
    /// `DeviceError::parameters` at assembly, a startup error in the
    /// server binary — so it names the offending parameter, channel, or
    /// register rather than a byte offset.
    pub fn parse(
        parameters: &BTreeMap<String, serde_json::Value>,
        channels: &BTreeMap<String, ValueKind>,
    ) -> Result<Self, String> {
        for name in parameters.keys() {
            if !matches!(name.as_str(), "address" | "timeout_ms" | "registers") {
                return Err(format!(
                    "unknown parameter {name:?}; {DEVICE_KIND:?} takes \"address\", \"timeout_ms\", and \"registers\""
                ));
            }
        }
        let address = parameters
            .get("address")
            .ok_or_else(|| "parameter \"address\" is required".to_string())?;
        let Some(address) = address.as_str() else {
            return Err(format!(
                "parameter \"address\" must be a string, found {address}"
            ));
        };
        let timeout = match parameters.get("timeout_ms") {
            None => BusDriver::DEFAULT_TIMEOUT,
            Some(value) => {
                let milliseconds = value.as_f64().filter(|ms| {
                    ms.is_finite() && ms.fract() == 0.0 && *ms >= 0.0 && *ms <= u64::MAX as f64
                });
                match milliseconds {
                    Some(ms) => Duration::from_millis(ms as u64),
                    None => {
                        return Err(format!(
                            "parameter \"timeout_ms\" must be a non-negative integral number of milliseconds, found {value}"
                        ));
                    }
                }
            }
        };

        let registers = parameters
            .get("registers")
            .ok_or_else(|| "parameter \"registers\" is required".to_string())?;
        let Some(registers) = registers.as_object() else {
            return Err(format!(
                "parameter \"registers\" must be an object mapping channel names to register declarations, found {registers}"
            ));
        };
        let mut parsed = BTreeMap::new();
        let mut taken: HashMap<u16, &str> = HashMap::new();
        for (channel, entry) in registers {
            let Some(&kind) = channels.get(channel.as_str()) else {
                return Err(format!(
                    "register map names channel {channel:?} the device does not declare"
                ));
            };
            let declaration = parse_register_entry(channel, kind, entry)?;
            if let Some(other) = taken.insert(declaration.register, channel.as_str()) {
                return Err(format!(
                    "channels {other:?} and {channel:?} share register {}",
                    declaration.register
                ));
            }
            parsed.insert(channel.clone(), declaration);
        }
        for channel in channels.keys() {
            if !parsed.contains_key(channel) {
                return Err(format!(
                    "register map does not cover declared channel {channel:?}"
                ));
            }
        }

        Ok(Self {
            address: address.to_string(),
            timeout,
            registers: parsed,
        })
    }
}

/// One `"registers"` entry: either a bare register address or
/// `{"register": <u16>, "initial": <value>}`.
fn parse_register_entry(
    channel: &str,
    kind: ValueKind,
    entry: &serde_json::Value,
) -> Result<ChannelRegister, String> {
    let invalid = |detail: String| {
        format!("register declaration for channel {channel:?}: {detail}")
    };
    // The shorthand form: `"level-raw": 4`.
    if let Some(register) = entry.as_u64() {
        let register = u16::try_from(register).map_err(|_| {
            invalid(format!("register address must fit in 0..=65535, found {register}"))
        })?;
        return Ok(ChannelRegister {
            register,
            initial: None,
        });
    }
    let Some(object) = entry.as_object() else {
        return Err(invalid(format!(
            "must be a register address or an object, found {entry}"
        )));
    };
    for key in object.keys() {
        if !matches!(key.as_str(), "register" | "initial") {
            return Err(invalid(format!("unknown key {key:?}")));
        }
    }
    let Some(register) = object.get("register") else {
        return Err(invalid("missing \"register\"".to_string()));
    };
    let Some(register) = register.as_u64() else {
        return Err(invalid(format!(
            "\"register\" must be a non-negative integer, found {register}"
        )));
    };
    let register = u16::try_from(register).map_err(|_| {
        invalid(format!(
            "register address must fit in 0..=65535, found {register}"
        ))
    })?;
    let initial = match object.get("initial") {
        None => None,
        Some(json) => {
            let value: Value = serde_json::from_value(json.clone()).map_err(|error| {
                invalid(format!("\"initial\" is not a signal value: {error}"))
            })?;
            if value.kind() != kind {
                return Err(invalid(format!(
                    "\"initial\" must match the channel's {kind:?} kind, found {json}"
                )));
            }
            Some(value)
        }
    };
    Ok(ChannelRegister { register, initial })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn channels() -> BTreeMap<String, ValueKind> {
        [
            ("pv".to_string(), ValueKind::Float),
            ("run".to_string(), ValueKind::Bool),
        ]
        .into_iter()
        .collect()
    }

    #[test]
    fn a_complete_declaration_parses() {
        let parameters = BTreeMap::from([
            ("address".to_string(), json!("127.0.0.1:5502")),
            ("timeout_ms".to_string(), json!(250)),
            (
                "registers".to_string(),
                json!({
                    "pv": {"register": 4, "initial": {"Float": 1.5}},
                    "run": 9
                }),
            ),
        ]);
        let parsed = DeviceParameters::parse(&parameters, &channels()).unwrap();
        assert_eq!(parsed.address, "127.0.0.1:5502");
        assert_eq!(parsed.timeout, Duration::from_millis(250));
        assert_eq!(
            parsed.registers["pv"],
            ChannelRegister {
                register: 4,
                initial: Some(Value::Float(1.5)),
            }
        );
        assert_eq!(
            parsed.registers["run"],
            ChannelRegister {
                register: 9,
                initial: None,
            }
        );
    }

    #[test]
    fn malformed_declarations_are_named_errors() {
        let cases = [
            // No address.
            json!({"registers": {"pv": 0, "run": 1}}),
            // Register map missing a declared channel.
            json!({"address": "a:1", "registers": {"pv": 0}}),
            // Register map naming an undeclared channel.
            json!({"address": "a:1", "registers": {"pv": 0, "run": 1, "xx": 2}}),
            // Two channels on one register.
            json!({"address": "a:1", "registers": {"pv": 0, "run": 0}}),
            // A non-integer register.
            json!({"address": "a:1", "registers": {"pv": "zero", "run": 1}}),
            // An initial whose kind disagrees with the channel.
            json!({"address": "a:1", "registers": {"pv": {"register": 0, "initial": {"Bool": true}}, "run": 1}}),
            // An unknown top-level parameter.
            json!({"address": "a:1", "registers": {"pv": 0, "run": 1}, "bogus": 1}),
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
}
