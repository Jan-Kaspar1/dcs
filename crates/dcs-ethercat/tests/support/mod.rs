//! The replay tests' shared rig: a captured bus, the deployment binding
//! that serves it, and the coupler declarations the captures are
//! verified against.
//!
//! Kept beside the captures rather than inside one case file so every
//! `replay-*.rs` target proves against the same declared mapping — the
//! point of a replay regression is that only the *recorded run* varies.

#![allow(dead_code)]

use dcs_core::{Direction, ValueKind};
use dcs_ethercat::{
    AttachError, BusPoint, Capture, ChannelDecl, DeviceParameters, DiscoveredStation,
    EthercatBuses, EthercatDevice,
};
use dcs_model::DeviceId;
use serde_json::json;
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Arc;

/// The checked-in capture directory, relative to this crate.
pub fn capture_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("captures")
}

/// The named checked-in capture — a missing or malformed file fails the
/// case by name rather than as a puzzling outcome later.
pub fn load_capture(name: &str) -> Capture {
    let path = capture_dir().join(format!("{name}.json"));
    Capture::load(&path).unwrap_or_else(|error| panic!("{}: {error}", path.display()))
}

/// The captured channel map: `di-1` at input bit 0, `ai-1` in input byte
/// 1, `do-1` at output bit 0, `ao-1` in output byte 1 — the coupler's
/// two-byte digital input and output areas.
pub fn channels() -> BTreeMap<String, ChannelDecl> {
    [
        ("di-1", Direction::In, ValueKind::Bool),
        ("ai-1", Direction::In, ValueKind::Int),
        ("do-1", Direction::Out, ValueKind::Bool),
        ("ao-1", Direction::Out, ValueKind::Int),
    ]
    .into_iter()
    .map(|(name, direction, kind)| (name.to_string(), ChannelDecl { direction, kind }))
    .collect()
}

/// The replayed device's bus points.
pub fn points() -> Vec<BusPoint> {
    [
        (10, "di-1", Direction::In, ValueKind::Bool),
        (11, "ai-1", Direction::In, ValueKind::Int),
        (12, "do-1", Direction::Out, ValueKind::Bool),
        (13, "ao-1", Direction::Out, ValueKind::Int),
    ]
    .into_iter()
    .map(|(point, channel, direction, kind)| BusPoint {
        point: dcs_core::PointId(point),
        channel: channel.to_string(),
        direction,
        kind,
    })
    .collect()
}

/// The Wago 750-354 coupler's declared identity — the identity the
/// checked-in captures record.
pub fn wago_identity() -> serde_json::Value {
    json!({"vendor": 6, "product": 354, "revision": 3})
}

/// The device's declared parameters on `bus`: the coupler's identity,
/// a miss threshold of 2, the channel mapping, the safe state, and the
/// failing startup policy.
pub fn parameters(bus: &str) -> BTreeMap<String, serde_json::Value> {
    serde_json::from_value(json!({
        "bus": bus,
        "identity": wago_identity(),
        "mapping": {
            "inputs": {"di-1": {"byte": 0, "bit": 0}, "ai-1": {"byte": 1, "bits": 8}},
            "outputs": {"do-1": {"byte": 0, "bit": 0}, "ao-1": {"byte": 1, "bits": 8}}
        },
        "exchange_miss_threshold": 2,
        "safe_outputs": {"do-1": {"bool": true}, "ao-1": {"int": 1}},
        "startup": {"on_mismatch": "fail"}
    }))
    .unwrap()
}

/// A second station on a captured bus, so a two-device case can bind it.
pub fn second_station(capture: &Capture) -> DiscoveredStation {
    DiscoveredStation {
        position: 1,
        name: capture.stations[0].name.clone(),
        vendor_id: capture.stations[0].vendor_id,
        product_id: capture.stations[0].product_id,
        revision: capture.stations[0].revision,
        input_bytes: capture.stations[0].input_bytes,
        output_bytes: capture.stations[0].output_bytes,
    }
}

/// A capture served through the deployment seam: one logical bus, bound
/// as a real deployment binds it, answered by the recording.
pub struct Rig {
    buses: EthercatBuses,
}

impl Rig {
    /// A rig serving `capture` on logical bus `bus`.
    pub fn over(capture: &Capture, bus: &str) -> Self {
        Self {
            buses: capture.buses(bus, "<capture>"),
        }
    }

    /// A rig serving `capture` on logical bus `bus` through an explicit
    /// binding map — a case that must prove the binding's own refusals.
    pub fn with_binding(capture: &Capture, bindings: BTreeMap<String, String>) -> Self {
        Self {
            buses: EthercatBuses::with_opener(bindings, capture.opener()),
        }
    }

    /// Attaches one model device with explicit declarations.
    pub fn attach(
        &self,
        id: u64,
        parameters: &BTreeMap<String, serde_json::Value>,
        channels: &BTreeMap<String, ChannelDecl>,
        points: &[BusPoint],
    ) -> Result<Arc<EthercatDevice>, AttachError> {
        let parsed = DeviceParameters::parse(parameters, channels).unwrap();
        self.buses.attach(DeviceId(id), &parsed, points)
    }

    /// One device on `bus` with the coupler's shared declarations.
    pub fn attach_on(&self, id: u64, bus: &str) -> Result<Arc<EthercatDevice>, AttachError> {
        self.attach(id, &parameters(bus), &channels(), &points())
    }

    /// The rig's declared device, attached.
    pub fn driver(&self, id: u64) -> Arc<EthercatDevice> {
        self.attach_on(id, "b0")
            .unwrap_or_else(|error| panic!("device {id} did not attach: {error}"))
    }
}
