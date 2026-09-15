//! The registry integration tests: a model declaring `kind:
//! "ethercat"` resolves through `resolve_drivers`/`FanoutDriver` like
//! the existing kinds, devices on one logical bus share one master —
//! one opener call, one exchange per scan — a second claim on one
//! bound interface is a named assembly failure, and the field-facing
//! honest-failover surface reports the devices.

use dcs_assembly::{AssemblyError, DriverRegistry, resolve_drivers};
use dcs_core::{IoDriver, PointId, Tick, Value};
use dcs_ethercat::testing::{FakeCycle, FakeLog, FakeTransport};
use dcs_ethercat::{DiscoveredStation, EthercatBuses, OpenRequest};
use dcs_model::{DeviceId, PlantModel};
use serde_json::json;
use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

fn station() -> DiscoveredStation {
    DiscoveredStation {
        position: 0,
        name: "st0".to_string(),
        vendor_id: 0xad,
        product_id: 950,
        revision: 2,
        input_bytes: 8,
        output_bytes: 2,
    }
}

/// The declared station profile both devices share.
fn stations() -> serde_json::Value {
    json!([{
        "position": 0,
        "vendor_id": "0xad",
        "product_id": 950,
        "revision": 2,
        "name": "st0",
        "input_bytes": 8,
        "output_bytes": 2
    }])
}

/// Device 1's parameters: input bit 0 in, output bit 0 out.
fn parameters_one(bus: &str) -> serde_json::Value {
    json!({
        "bus": bus,
        "exchange_miss_threshold": 2,
        "stations": stations(),
        "layout": {
            "di-1": {"station": 0, "offset": 0, "bit": 0},
            "do-1": {"station": 0, "offset": 0, "bit": 0}
        }
    })
}

/// Device 2's parameters: input bit 1 in, output bit 1 out — the
/// output ranges must not overlap device 1's claim.
fn parameters_two(bus: &str) -> serde_json::Value {
    json!({
        "bus": bus,
        "exchange_miss_threshold": 2,
        "stations": stations(),
        "layout": {
            "di-2": {"station": 0, "offset": 0, "bit": 1},
            "do-2": {"station": 0, "offset": 0, "bit": 1}
        }
    })
}

fn device(
    id: u64,
    parameters: serde_json::Value,
    channels: serde_json::Value,
) -> serde_json::Value {
    json!({
        "id": id,
        "kind": "ethercat",
        "channels": channels,
        "parameters": parameters
    })
}

/// A model with two `ethercat` devices: `bus_one` on device 1 and
/// `bus_two` on device 2 — the same string when sharing a bus.
fn two_device_model(bus_one: &str, bus_two: &str) -> PlantModel {
    PlantModel::load(
        &json!({
            "version": 1,
            "devices": [
                device(1, parameters_one(bus_one), json!({
                    "di-1": {"direction": "in", "value_type": "bool"},
                    "do-1": {"direction": "out", "value_type": "bool"}
                })),
                device(2, parameters_two(bus_two), json!({
                    "di-2": {"direction": "in", "value_type": "bool"},
                    "do-2": {"direction": "out", "value_type": "bool"}
                }))
            ],
            "io_points": [
                {"id": 10, "direction": "in", "value_type": "bool", "channel": {"device": 1, "name": "di-1"}},
                {"id": 11, "direction": "in", "value_type": "bool", "channel": {"device": 2, "name": "di-2"}},
                {"id": 12, "direction": "out", "value_type": "bool", "channel": {"device": 1, "name": "do-1"}},
                {"id": 13, "direction": "out", "value_type": "bool", "channel": {"device": 2, "name": "do-2"}}
            ],
            "signals": [],
            "components": [],
            "connections": []
        })
        .to_string(),
    )
    .unwrap()
}

/// A standard registry with the `ethercat` kind bound to `bindings`,
/// plus the fakes' shared call log and open count.
fn registry(
    bindings: &[(&str, &str)],
    script: Vec<FakeCycle>,
) -> (DriverRegistry, Arc<Mutex<FakeLog>>, Arc<AtomicUsize>) {
    let log = Arc::new(Mutex::new(FakeLog::default()));
    let opens = Arc::new(AtomicUsize::new(0));
    let script = Mutex::new(Some(VecDeque::from(script)));
    let buses = EthercatBuses::with_opener(
        bindings
            .iter()
            .map(|(bus, interface)| (bus.to_string(), interface.to_string()))
            .collect::<HashMap<_, _>>(),
        {
            let log = Arc::clone(&log);
            let opens = Arc::clone(&opens);
            Arc::new(move |_request: &OpenRequest<'_>| {
                opens.fetch_add(1, Ordering::SeqCst);
                Ok(Box::new(FakeTransport::with_log(
                    vec![station()],
                    script.lock().unwrap().take().unwrap_or_default(),
                    Arc::clone(&log),
                )) as Box<dyn dcs_ethercat::BusTransport>)
            })
        },
    );
    let mut registry = DriverRegistry::standard();
    buses.register(&mut registry);
    (registry, log, opens)
}

#[test]
fn the_kind_resolves_and_exchanges_through_the_fanout() {
    let (registry, log, opens) = registry(
        &[("b0", "eth0")],
        vec![FakeCycle::complete(vec![0b11, 0, 0, 0, 0, 0, 0, 0])],
    );
    let model = two_device_model("b0", "b0");
    let driver = resolve_drivers(&model, &registry).unwrap().build().unwrap();

    // One master serves both devices: one open, one cyclic exchange
    // per scan — the fan-out calls each backend's cyclic surface, and
    // only the bus owner answers.
    let cyclic = driver.cyclic().expect("the bus owner reports cyclic");
    cyclic.exchange(Tick(7)).unwrap();
    assert_eq!(log.lock().unwrap().exchanges, 1);
    assert_eq!(opens.load(Ordering::SeqCst), 1);

    // Both devices' inputs latched at the exchange tick.
    assert_eq!(driver.read(PointId(10)).unwrap().tick, Tick(7));
    assert_eq!(driver.read(PointId(11)).unwrap().tick, Tick(7));
    assert_eq!(driver.read(PointId(11)).unwrap().value, Value::Bool(true));

    // The aggregate diagnostics carry the bus's exchange record once.
    let diagnostics = driver.diagnostics().unwrap();
    let exchange = diagnostics.exchange.unwrap();
    assert_eq!(exchange.attempted, 1);
    assert_eq!(exchange.succeeded, 1);
    assert_eq!(exchange.last_exchange_tick, Some(Tick(7)));

    // Field-observing kind: no captured state, and without a
    // single-writer arbitration both devices honestly list as
    // unfenceable for automatic failover.
    assert!(driver.capture_state().is_none());
    let mut unfenced = driver.unfenced_field_devices();
    unfenced.sort();
    assert_eq!(unfenced, vec![DeviceId(1), DeviceId(2)]);
}

#[test]
fn a_second_claim_on_one_interface_is_a_named_failure() {
    // Two logical buses, one interface: the deployment said both live
    // on eth0, which only one master may bind.
    let (registry, _, _) = registry(&[("b0", "eth0"), ("b1", "eth0")], vec![]);
    let model = two_device_model("b0", "b1");
    let error = resolve_drivers(&model, &registry).err().unwrap();
    match error {
        AssemblyError::DeviceBackend { device, detail, .. } => {
            assert_eq!(device, DeviceId(2));
            assert!(detail.contains("already claimed"), "{detail}");
        }
        other => panic!("expected DeviceBackend, got {other:?}"),
    }
}

#[test]
fn an_unbound_logical_bus_is_a_named_failure() {
    // The model names a bus the deployment never bound — the error
    // names the device, and nothing reached an interface.
    let (registry, log, _) = registry(&[], vec![]);
    let model = two_device_model("b0", "b0");
    let error = resolve_drivers(&model, &registry).err().unwrap();
    match error {
        AssemblyError::DeviceBackend { device, detail, .. } => {
            assert_eq!(device, DeviceId(1));
            assert!(
                detail.contains("no deployment interface binding"),
                "{detail}"
            );
        }
        other => panic!("expected DeviceBackend, got {other:?}"),
    }
    assert_eq!(log.lock().unwrap().exchanges, 0);
}

#[test]
fn the_cyclic_surface_is_optional_across_mixed_kinds() {
    // `resolve_drivers` composes the kind with the standard registry's
    // kinds — a mixed model still assembles, the ethercat devices
    // carrying the only cyclic surface.
    let (registry, log, _) = registry(
        &[("b0", "eth0")],
        vec![FakeCycle::complete(vec![0b1, 0, 0, 0, 0, 0, 0, 0])],
    );
    let model = PlantModel::load(
        &json!({
            "version": 1,
            "devices": [
                device(1, parameters_one("b0"), json!({
                    "di-1": {"direction": "in", "value_type": "bool"},
                    "do-1": {"direction": "out", "value_type": "bool"}
                })),
                {
                    "id": 9,
                    "kind": "sim",
                    "channels": {"level": {"direction": "in", "value_type": "float"}},
                    "parameters": {}
                }
            ],
            "io_points": [
                {"id": 10, "direction": "in", "value_type": "bool", "channel": {"device": 1, "name": "di-1"}},
                {"id": 12, "direction": "out", "value_type": "bool", "channel": {"device": 1, "name": "do-1"}},
                {"id": 90, "direction": "in", "value_type": "float", "channel": {"device": 9, "name": "level"}}
            ],
            "signals": [],
            "components": [],
            "connections": []
        })
        .to_string(),
    )
    .unwrap();
    let driver = resolve_drivers(&model, &registry).unwrap().build().unwrap();
    driver.cyclic().unwrap().exchange(Tick(3)).unwrap();
    assert_eq!(log.lock().unwrap().exchanges, 1);
    assert_eq!(driver.read(PointId(10)).unwrap().value, Value::Bool(true));
}
