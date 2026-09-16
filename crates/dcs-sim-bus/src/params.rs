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
use crate::RegisterDecl;
use crate::cyclic::CyclicBusDriver;
use dcs_core::{Value, ValueKind};
use std::collections::{BTreeMap, BTreeSet, HashMap};
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

/// The device-kind string the `sim-cyclic` integration serves —
/// registered in `dcs-assembly`'s `DriverRegistry::standard` as
/// `SIM_CYCLIC_KIND` and matched by the `dcs-sim-bus-device` binary
/// when it selects a device from a model.
pub const CYCLIC_DEVICE_KIND: &str = "sim-cyclic";

/// A `sim-cyclic` device's parsed `parameters`.
///
/// Where [`DeviceParameters`] maps every channel onto one register for
/// the point-wise protocol, a cyclic device declares its register image
/// as **stations** — the register banks a real cyclic bus attributes a
/// short exchange's working counter to — plus the
/// `exchange_miss_threshold` the driver's escalation reads.
///
/// A `sim-cyclic` device's `parameters` carry:
///
/// - `"address"` (required string): the device server's `host:port` —
///   where the [`CyclicBusDriver`] connects and the default `--listen`
///   for the server binary;
/// - `"timeout_ms"` (optional non-negative integral number): the
///   per-request timeout in milliseconds, defaulting to
///   [`CyclicBusDriver::DEFAULT_TIMEOUT`];
/// - `"exchange_miss_threshold"` (required positive integer): the
///   consecutive missed exchanges before the driver's reads escalate to
///   `IoError::Disconnected`;
/// - `"stations"` (required object): station name → channel name →
///   register declaration — the same entry grammar `"registers"`
///   carries — partitioning the device's declared channels: every
///   channel appears in exactly one station, every station holds at
///   least one channel, and no two channels share a register.
#[derive(Debug, Clone, PartialEq)]
pub struct CyclicDeviceParameters {
    /// The device server's `host:port`, as declared.
    pub address: String,
    /// The per-request timeout for driver exchanges.
    pub timeout: Duration,
    /// The consecutive missed exchanges the driver tolerates before
    /// escalating reads to `IoError::Disconnected` — at least 1.
    pub exchange_miss_threshold: u64,
    /// Station name → channel name → register declaration — the
    /// station layout the device's register image is attributed to.
    pub stations: BTreeMap<String, BTreeMap<String, ChannelRegister>>,
}

impl CyclicDeviceParameters {
    /// Parses and validates a `sim-cyclic` device's `parameters`
    /// against its declared `channels` (name → `value_type`), under the
    /// same named-error contract [`DeviceParameters::parse`] documents.
    pub fn parse(
        parameters: &BTreeMap<String, serde_json::Value>,
        channels: &BTreeMap<String, ValueKind>,
    ) -> Result<Self, String> {
        for name in parameters.keys() {
            if !matches!(
                name.as_str(),
                "address" | "timeout_ms" | "exchange_miss_threshold" | "stations"
            ) {
                return Err(format!(
                    "unknown parameter {name:?}; {CYCLIC_DEVICE_KIND:?} takes \"address\", \"timeout_ms\", \"exchange_miss_threshold\", and \"stations\""
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
            None => CyclicBusDriver::DEFAULT_TIMEOUT,
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
        let exchange_miss_threshold = match parameters.get("exchange_miss_threshold") {
            None => {
                return Err("parameter \"exchange_miss_threshold\" is required".to_string());
            }
            Some(value) => match value.as_u64() {
                Some(threshold) if threshold >= 1 => threshold,
                _ => {
                    return Err(format!(
                        "parameter \"exchange_miss_threshold\" must be a positive integer, found {value}"
                    ));
                }
            },
        };

        let stations = parameters
            .get("stations")
            .ok_or_else(|| "parameter \"stations\" is required".to_string())?;
        let Some(stations) = stations.as_object() else {
            return Err(format!(
                "parameter \"stations\" must be an object mapping station names to channel register declarations, found {stations}"
            ));
        };
        if stations.is_empty() {
            return Err(
                "parameter \"stations\" must declare at least one station".to_string(),
            );
        }
        let mut parsed: BTreeMap<String, BTreeMap<String, ChannelRegister>> = BTreeMap::new();
        let mut placed: HashMap<&str, &str> = HashMap::new();
        let mut taken: HashMap<u16, &str> = HashMap::new();
        for (station, layout) in stations {
            if station.is_empty() {
                return Err("parameter \"stations\" names a station with an empty name".to_string());
            }
            let Some(layout) = layout.as_object() else {
                return Err(format!(
                    "station {station:?} must be an object mapping channel names to register declarations, found {layout}"
                ));
            };
            if layout.is_empty() {
                return Err(format!("station {station:?} declares no channels"));
            }
            let mut bank = BTreeMap::new();
            for (channel, entry) in layout {
                let Some(&kind) = channels.get(channel.as_str()) else {
                    return Err(format!(
                        "station {station:?} names channel {channel:?} the device does not declare"
                    ));
                };
                if let Some(other) = placed.insert(channel.as_str(), station.as_str()) {
                    return Err(format!(
                        "stations {other:?} and {station:?} both place channel {channel:?}"
                    ));
                }
                let declaration = parse_register_entry(channel, kind, entry)?;
                if let Some(other) = taken.insert(declaration.register, channel.as_str()) {
                    return Err(format!(
                        "channels {other:?} and {channel:?} share register {}",
                        declaration.register
                    ));
                }
                bank.insert(channel.clone(), declaration);
            }
            parsed.insert(station.clone(), bank);
        }
        for channel in channels.keys() {
            if !placed.contains_key(channel.as_str()) {
                return Err(format!(
                    "station layout does not place declared channel {channel:?}"
                ));
            }
        }

        Ok(Self {
            address: address.to_string(),
            timeout,
            exchange_miss_threshold,
            stations: parsed,
        })
    }

    /// The station and register declaration placing `channel`, or
    /// `None` when the channel is not placed — impossible for a channel
    /// the device declares, which `parse` requires the layout to cover.
    pub fn channel_register(&self, channel: &str) -> Option<(&str, ChannelRegister)> {
        self.stations.iter().find_map(|(station, bank)| {
            bank.get(channel)
                .map(|&declaration| (station.as_str(), declaration))
        })
    }

    /// The station attribution map the server and driver share:
    /// station name → the register addresses its channels occupy.
    pub fn station_registers(&self) -> BTreeMap<String, BTreeSet<u16>> {
        self.stations
            .iter()
            .map(|(station, bank)| {
                (
                    station.clone(),
                    bank.values().map(|decl| decl.register).collect(),
                )
            })
            .collect()
    }

    /// The register-bank declarations a `dcs-sim-bus-device` server
    /// opens for this device: one [`RegisterDecl`] per station channel,
    /// seeded by its declared `initial` or the channel kind's neutral
    /// value.
    pub fn register_decls(&self, channels: &BTreeMap<String, ValueKind>) -> Vec<RegisterDecl> {
        self.stations
            .values()
            .flat_map(|bank| bank.iter())
            .map(|(channel, declaration)| RegisterDecl {
                register: declaration.register,
                initial: declaration
                    .initial
                    .unwrap_or_else(|| neutral(channels[channel])),
            })
            .collect()
    }
}

/// The value a register holds before anything writes it — the kind's
/// zero.
fn neutral(kind: ValueKind) -> Value {
    match kind {
        ValueKind::Bool => Value::Bool(false),
        ValueKind::Int => Value::Int(0),
        ValueKind::Float => Value::Float(0.0),
    }
}

/// One `"registers"` entry: either a bare register address or
/// `{"register": <u16>, "initial": <value>}`.
fn parse_register_entry(
    channel: &str,
    kind: ValueKind,
    entry: &serde_json::Value,
) -> Result<ChannelRegister, String> {
    let invalid =
        |detail: String| format!("register declaration for channel {channel:?}: {detail}");
    // The shorthand form: `"level-raw": 4`.
    if let Some(register) = entry.as_u64() {
        let register = u16::try_from(register).map_err(|_| {
            invalid(format!(
                "register address must fit in 0..=65535, found {register}"
            ))
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
            let value: Value = serde_json::from_value(json.clone())
                .map_err(|error| invalid(format!("\"initial\" is not a signal value: {error}")))?;
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
                    "pv": {"register": 4, "initial": {"float": 1.5}},
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
            json!({"address": "a:1", "registers": {"pv": {"register": 0, "initial": {"bool": true}}, "run": 1}}),
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

    /// A cyclic channel set: two floats on `inlet`, a bool on
    /// `outlet`.
    fn cyclic_channels() -> BTreeMap<String, ValueKind> {
        [
            ("level".to_string(), ValueKind::Float),
            ("flow".to_string(), ValueKind::Float),
            ("run".to_string(), ValueKind::Bool),
        ]
        .into_iter()
        .collect()
    }

    /// The cyclic station layout covering [`cyclic_channels`].
    fn cyclic_stations() -> serde_json::Value {
        json!({
            "inlet": {
                "level": {"register": 4, "initial": {"float": 12.0}},
                "flow": 5
            },
            "outlet": {
                "run": 8
            }
        })
    }

    #[test]
    fn a_complete_cyclic_declaration_parses() {
        let parameters = BTreeMap::from([
            ("address".to_string(), json!("127.0.0.1:5502")),
            ("timeout_ms".to_string(), json!(250)),
            ("exchange_miss_threshold".to_string(), json!(3)),
            ("stations".to_string(), cyclic_stations()),
        ]);
        let parsed = CyclicDeviceParameters::parse(&parameters, &cyclic_channels()).unwrap();
        assert_eq!(parsed.address, "127.0.0.1:5502");
        assert_eq!(parsed.timeout, Duration::from_millis(250));
        assert_eq!(parsed.exchange_miss_threshold, 3);
        assert_eq!(
            parsed.channel_register("level"),
            Some((
                "inlet",
                ChannelRegister {
                    register: 4,
                    initial: Some(Value::Float(12.0)),
                },
            ))
        );
        assert_eq!(
            parsed.channel_register("run"),
            Some((
                "outlet",
                ChannelRegister {
                    register: 8,
                    initial: None,
                },
            ))
        );
        assert_eq!(
            parsed.station_registers(),
            BTreeMap::from([
                ("inlet".to_string(), BTreeSet::from([4, 5])),
                ("outlet".to_string(), BTreeSet::from([8])),
            ])
        );
        // The bank declarations seed initials where declared and the
        // kind's neutral elsewhere.
        assert_eq!(
            parsed.register_decls(&cyclic_channels()),
            vec![
                RegisterDecl {
                    register: 4,
                    initial: Value::Float(12.0),
                },
                RegisterDecl {
                    register: 5,
                    initial: Value::Float(0.0),
                },
                RegisterDecl {
                    register: 8,
                    initial: Value::Bool(false),
                },
            ]
        );
    }

    #[test]
    fn malformed_cyclic_declarations_are_named_errors() {
        let base = || {
            BTreeMap::from([
                ("address".to_string(), json!("a:1")),
                ("exchange_miss_threshold".to_string(), json!(3)),
                ("stations".to_string(), cyclic_stations()),
            ])
        };
        let cases = [
            // No address.
            serde_json::from_value::<BTreeMap<String, serde_json::Value>>(json!({
                "exchange_miss_threshold": 3,
                "stations": {"inlet": {"level": 4, "flow": 5}, "outlet": {"run": 8}}
            }))
            .unwrap(),
            // No miss threshold.
            {
                let mut parameters = base();
                parameters.remove("exchange_miss_threshold");
                parameters
            },
            // A zero miss threshold tolerates nothing.
            serde_json::from_value::<BTreeMap<String, serde_json::Value>>(json!({
                "address": "a:1",
                "exchange_miss_threshold": 0,
                "stations": {"inlet": {"level": 4, "flow": 5}, "outlet": {"run": 8}}
            }))
            .unwrap(),
            // Stations absent.
            {
                let mut parameters = base();
                parameters.remove("stations");
                parameters
            },
            // An empty station layout.
            serde_json::from_value::<BTreeMap<String, serde_json::Value>>(json!({
                "address": "a:1",
                "exchange_miss_threshold": 3,
                "stations": {}
            }))
            .unwrap(),
            // A station holding no channels.
            serde_json::from_value::<BTreeMap<String, serde_json::Value>>(json!({
                "address": "a:1",
                "exchange_miss_threshold": 3,
                "stations": {"inlet": {"level": 4, "flow": 5}, "outlet": {}}
            }))
            .unwrap(),
            // A station naming an undeclared channel.
            serde_json::from_value::<BTreeMap<String, serde_json::Value>>(json!({
                "address": "a:1",
                "exchange_miss_threshold": 3,
                "stations": {"inlet": {"level": 4, "flow": 5, "xx": 6}, "outlet": {"run": 8}}
            }))
            .unwrap(),
            // A channel placed in two stations.
            serde_json::from_value::<BTreeMap<String, serde_json::Value>>(json!({
                "address": "a:1",
                "exchange_miss_threshold": 3,
                "stations": {"inlet": {"level": 4, "flow": 5}, "outlet": {"run": 8, "level": 9}}
            }))
            .unwrap(),
            // A declared channel no station places.
            serde_json::from_value::<BTreeMap<String, serde_json::Value>>(json!({
                "address": "a:1",
                "exchange_miss_threshold": 3,
                "stations": {"inlet": {"level": 4}, "outlet": {"run": 8}}
            }))
            .unwrap(),
            // Two channels sharing a register across stations.
            serde_json::from_value::<BTreeMap<String, serde_json::Value>>(json!({
                "address": "a:1",
                "exchange_miss_threshold": 3,
                "stations": {"inlet": {"level": 4, "flow": 5}, "outlet": {"run": 4}}
            }))
            .unwrap(),
            // An unknown top-level parameter.
            {
                let mut parameters = base();
                parameters.insert("bogus".to_string(), json!(1));
                parameters
            },
        ];
        for parameters in cases {
            assert!(
                CyclicDeviceParameters::parse(&parameters, &cyclic_channels()).is_err(),
                "{parameters:?}"
            );
        }
    }
}
