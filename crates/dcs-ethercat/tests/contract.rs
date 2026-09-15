//! The cyclic-contract tests: `ethercat` devices over a scripted fake
//! transport prove the exchange semantics the issue requires — one
//! exchange per call, image-local reads and writes, latched freshness,
//! failure aging and escalation, staged-output retention, working-
//! counter attribution, boundary recovery, and diagnostics — without
//! hardware.

use dcs_core::{Direction, IoDriver, IoError, LinkState, PointId, Tick, Value, ValueKind};
use dcs_ethercat::testing::{FakeCycle, FakeLog, FakeTransport};
use dcs_ethercat::{
    AttachError, BusPoint, ChannelDecl, DeviceParameters, DiscoveredStation, EthercatBuses,
    EthercatDevice, OpenRequest, TransportError,
};
use dcs_model::DeviceId;
use serde_json::json;
use std::collections::{BTreeMap, VecDeque};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

/// The fake bus: one station with 8 input bytes and 2 output bytes.
fn discovered() -> Vec<DiscoveredStation> {
    vec![DiscoveredStation {
        position: 0,
        name: "st0".to_string(),
        vendor_id: 0xad,
        product_id: 950,
        revision: 2,
        input_bytes: 8,
        output_bytes: 2,
    }]
}

/// The declared identity matching [`discovered`]'s station.
fn identity() -> serde_json::Value {
    json!({"vendor": 0xad, "product": 950, "revision": 2})
}

/// The device's declared parameters — the identity matching
/// [`discovered`], a miss threshold of 2, and the channel mapping:
/// `di-1` at input bit 0, `ai-1` at input bytes 1..5, `do-1` at output
/// bit 0, `ao-1` at output byte 1. The safe state stages `do-1` high
/// and `ao-1` at 7.
fn parameters(bus: &str) -> BTreeMap<String, serde_json::Value> {
    serde_json::from_value(json!({
        "bus": bus,
        "identity": identity(),
        "mapping": {
            "inputs": {
                "di-1": {"byte": 0, "bit": 0},
                "ai-1": {"byte": 1, "bits": 32}
            },
            "outputs": {
                "do-1": {"byte": 0, "bit": 0},
                "ao-1": {"byte": 1, "bits": 8}
            }
        },
        "exchange_miss_threshold": 2,
        "safe_outputs": {"do-1": {"bool": true}, "ao-1": {"int": 7}},
        "startup": {"on_mismatch": "fail"}
    }))
    .unwrap()
}

fn channels() -> BTreeMap<String, ChannelDecl> {
    [
        ("di-1", Direction::In, ValueKind::Bool),
        ("ai-1", Direction::In, ValueKind::Float),
        ("do-1", Direction::Out, ValueKind::Bool),
        ("ao-1", Direction::Out, ValueKind::Int),
    ]
    .into_iter()
    .map(|(name, direction, kind)| (name.to_string(), ChannelDecl { direction, kind }))
    .collect()
}

fn points() -> Vec<BusPoint> {
    [
        (10, "di-1", Direction::In, ValueKind::Bool),
        (11, "ai-1", Direction::In, ValueKind::Float),
        (12, "do-1", Direction::Out, ValueKind::Bool),
        (13, "ao-1", Direction::Out, ValueKind::Int),
    ]
    .into_iter()
    .map(|(point, channel, direction, kind)| BusPoint {
        point: PointId(point),
        channel: channel.to_string(),
        direction,
        kind,
    })
    .collect()
}

/// A test rig: an [`EthercatBuses`] whose opener builds scripted
/// fakes, plus the fake's shared call log and open count.
struct Rig {
    buses: EthercatBuses,
    log: Arc<Mutex<FakeLog>>,
    opens: Arc<AtomicUsize>,
    parameters: BTreeMap<String, serde_json::Value>,
    channels: BTreeMap<String, ChannelDecl>,
    points: Vec<BusPoint>,
}

impl Rig {
    /// Attaches one model device onto the rig's bus with explicit
    /// declarations — multi-device tests pass each device's own.
    fn attach(
        &self,
        id: u64,
        parameters: &BTreeMap<String, serde_json::Value>,
        channels: &BTreeMap<String, ChannelDecl>,
        points: &[BusPoint],
    ) -> Result<Arc<EthercatDevice>, AttachError> {
        let parsed = DeviceParameters::parse(parameters, channels).unwrap();
        self.buses.attach(DeviceId(id), &parsed, points)
    }

    /// Attaches one device with the rig's shared declarations.
    fn device(&self, id: u64) -> Result<Arc<EthercatDevice>, AttachError> {
        self.attach(id, &self.parameters, &self.channels, &self.points)
    }

    /// The rig's shared declarations, attached — most tests drive one
    /// device.
    fn driver(&self, id: u64) -> Arc<EthercatDevice> {
        self.device(id).unwrap()
    }
}

/// A rig over one fake bus on `eth0` running `script`.
fn rig(script: Vec<FakeCycle>) -> Rig {
    rig_fakes(discovered(), script, |_| {})
}

/// A rig whose opener customizes each fake before serving it — e.g.
/// scripting `enter_op` or `recover` to refuse.
fn rig_fakes(
    stations: Vec<DiscoveredStation>,
    script: Vec<FakeCycle>,
    setup: impl Fn(&mut FakeTransport) + Send + Sync + 'static,
) -> Rig {
    let log = Arc::new(Mutex::new(FakeLog::default()));
    let opens = Arc::new(AtomicUsize::new(0));
    let stations = Arc::new(stations);
    let script = Arc::new(Mutex::new(Some(VecDeque::from(script))));
    let setup = Mutex::new(Some(setup));
    let buses =
        EthercatBuses::with_opener(BTreeMap::from([("b0".to_string(), "eth0".to_string())]), {
            let log = Arc::clone(&log);
            let opens = Arc::clone(&opens);
            Arc::new(move |request: &OpenRequest<'_>| {
                opens.fetch_add(1, Ordering::SeqCst);
                assert_eq!(request.bus, "b0");
                assert_eq!(request.interface, "eth0");
                // The open request carries the first attacher's
                // declared identity — the only station the model has
                // named so far.
                assert_eq!(request.expected.len(), 1);
                let mut fake = FakeTransport::with_log(
                    stations.to_vec(),
                    script.lock().unwrap().take().unwrap_or_default(),
                    Arc::clone(&log),
                );
                // The scripted setup applies to the first opened bus —
                // a retry after a failed open gets a clean fake.
                if let Some(setup) = setup.lock().unwrap().take() {
                    setup(&mut fake);
                }
                Ok(Box::new(fake))
            })
        });
    Rig {
        buses,
        log,
        opens,
        parameters: parameters("b0"),
        channels: channels(),
        points: points(),
    }
}

/// The input image bytes for `di-1` and `ai-1`: `di` in bit 0, `ai` as
/// a little-endian f32 in bytes 1..5, zeroes elsewhere.
fn input_image(di: bool, ai: f32) -> Vec<u8> {
    let mut image = vec![0u8; 8];
    image[0] = u8::from(di);
    image[1..5].copy_from_slice(&ai.to_le_bytes());
    image
}

#[test]
fn identity_mismatch_fails_before_op() {
    let mut wrong = discovered();
    wrong[0].vendor_id = 0xbeef;
    let rig = rig_fakes(wrong, vec![], |_| {});
    let error = rig.device(1).err().unwrap();
    assert!(matches!(error, AttachError::Backend(_)), "{error:?}");
    assert!(error.to_string().contains("vendor"), "{error}");
    // Verification ran before OP entry — no cyclic surface opened.
    let log = rig.log.lock().unwrap();
    assert_eq!(log.enter_ops, 0);
    assert_eq!(log.exchanges, 0);
}

#[test]
fn layout_mismatch_fails_before_op() {
    let mut wrong = discovered();
    wrong[0].input_bytes = 4;
    let rig = rig_fakes(wrong, vec![], |_| {});
    let error = rig.device(1).err().unwrap();
    assert!(matches!(error, AttachError::Backend(_)), "{error:?}");
    assert!(error.to_string().contains("input area"), "{error}");
    assert_eq!(rig.log.lock().unwrap().enter_ops, 0);
}

#[test]
fn op_entry_refusal_is_a_backend_failure_and_frees_the_bus() {
    let rig = rig_fakes(discovered(), vec![], |fake| {
        fake.fail_enter_op(TransportError::State("station 0 refused OP".to_string()));
    });
    let error = rig.device(1).err().unwrap();
    assert!(matches!(error, AttachError::Backend(_)), "{error:?}");
    assert!(error.to_string().contains("OP"), "{error}");
    // The failed open released the interface claim: a retry opens the
    // bus again rather than poisoning the binding.
    rig.device(2).unwrap();
    assert_eq!(rig.opens.load(Ordering::SeqCst), 2);
}

#[test]
fn read_and_write_never_touch_the_transport() {
    let rig = rig(vec![]);
    let driver = rig.driver(1);
    // Before any exchange: inputs serve the neutral latched image at
    // Tick::ZERO, outputs the seeded staged image.
    assert_eq!(driver.read(PointId(10)).unwrap().value, Value::Bool(false));
    assert_eq!(driver.read(PointId(10)).unwrap().tick, Tick::ZERO);
    assert_eq!(driver.read(PointId(12)).unwrap().value, Value::Bool(true));
    assert_eq!(driver.read(PointId(13)).unwrap().value, Value::Int(7));
    driver.write(PointId(13), Value::Int(9)).unwrap();
    assert_eq!(driver.read(PointId(13)).unwrap().value, Value::Int(9));
    // Nothing reached the wire — no exchange ran.
    let log = rig.log.lock().unwrap();
    assert_eq!(log.exchanges, 0);
    assert_eq!(log.recovers, 0);
}

#[test]
fn one_exchange_per_call_latches_at_the_scan_tick() {
    let rig = rig(vec![
        FakeCycle::complete(input_image(true, 2.5)),
        FakeCycle::complete(input_image(false, 9.0)),
    ]);
    let driver = rig.driver(1);
    let cyclic = driver.cyclic().expect("the bus owner is cyclic");
    cyclic.exchange(Tick(5)).unwrap();
    let di = driver.read(PointId(10)).unwrap();
    assert_eq!(di.value, Value::Bool(true));
    assert_eq!(di.tick, Tick(5));
    assert_eq!(driver.read(PointId(11)).unwrap().value, Value::Float(2.5));
    cyclic.exchange(Tick(6)).unwrap();
    let di = driver.read(PointId(10)).unwrap();
    assert_eq!(di.value, Value::Bool(false));
    assert_eq!(di.tick, Tick(6));
    // One exchange per call — the read path added none.
    assert_eq!(rig.log.lock().unwrap().exchanges, 2);
}

#[test]
fn the_safe_state_seeds_the_first_published_image() {
    let rig = rig(vec![FakeCycle::complete(input_image(false, 0.0))]);
    let driver = rig.driver(1);
    driver.cyclic().unwrap().exchange(Tick(1)).unwrap();
    // Before any write, the staged image carried the declared safe
    // state: do-1 high in output byte 0 bit 0, ao-1 = 7 in byte 1.
    assert_eq!(rig.log.lock().unwrap().published[0], vec![0b1, 7]);
}

#[test]
fn staged_outputs_publish_once_and_survive_failed_exchanges() {
    let rig = rig(vec![
        FakeCycle::failed(TransportError::disconnected("link flap")),
        FakeCycle::complete(input_image(false, 0.0)),
    ]);
    let driver = rig.driver(1);
    driver.write(PointId(12), Value::Bool(true)).unwrap();
    driver.write(PointId(13), Value::Int(9)).unwrap();
    let cyclic = driver.cyclic().unwrap();
    // The failed exchange names a covered point and leaves the staged
    // image intact.
    assert_eq!(
        cyclic.exchange(Tick(1)),
        Err(IoError::Disconnected(PointId(10)))
    );
    assert_eq!(driver.read(PointId(12)).unwrap().value, Value::Bool(true));
    assert_eq!(driver.read(PointId(13)).unwrap().value, Value::Int(9));
    // The next completed exchange publishes the retained staging.
    cyclic.exchange(Tick(2)).unwrap();
    let log = rig.log.lock().unwrap();
    assert_eq!(log.exchanges, 2);
    assert_eq!(log.published[1], vec![0b1, 9]);
}

#[test]
fn failures_age_then_escalate_at_the_declared_threshold() {
    let rig = rig(vec![
        FakeCycle::complete(input_image(true, 1.0)),
        FakeCycle::failed(TransportError::disconnected("frame lost")),
        FakeCycle::failed(TransportError::disconnected("frame lost")),
        FakeCycle::complete(input_image(false, 3.0)),
    ]);
    let driver = rig.driver(1);
    let cyclic = driver.cyclic().unwrap();
    cyclic.exchange(Tick(1)).unwrap();
    assert_eq!(driver.read(PointId(10)).unwrap().value, Value::Bool(true));
    // One miss: the held image still serves.
    cyclic.exchange(Tick(2)).unwrap_err();
    let held = driver.read(PointId(10)).unwrap();
    assert_eq!(held.value, Value::Bool(true));
    assert_eq!(held.tick, Tick(1));
    // The second miss hits the declared threshold of 2.
    cyclic.exchange(Tick(3)).unwrap_err();
    assert_eq!(
        driver.read(PointId(10)),
        Err(IoError::Disconnected(PointId(10)))
    );
    // A clean exchange at the next boundary clears the miss count.
    cyclic.exchange(Tick(4)).unwrap();
    let fresh = driver.read(PointId(10)).unwrap();
    assert_eq!(fresh.value, Value::Bool(false));
    assert_eq!(fresh.tick, Tick(4));
}

#[test]
fn recovery_reenters_at_the_exchange_boundary_only() {
    let rig = rig_fakes(
        discovered(),
        vec![
            FakeCycle::failed(TransportError::disconnected("station dropped")),
            FakeCycle::complete(input_image(true, 4.0)),
        ],
        // The first re-entry refuses; the second succeeds — the fake
        // consumes its scripted refusal once.
        |fake| {
            fake.fail_recover(TransportError::State("still SAFE-OP".to_string()));
        },
    );
    let driver = rig.driver(1);
    let cyclic = driver.cyclic().unwrap();
    cyclic.exchange(Tick(1)).unwrap_err();
    // The next boundary runs the re-entry first: it fails, so no
    // exchange ran at all this scan — aging continues.
    cyclic.exchange(Tick(2)).unwrap_err();
    {
        let log = rig.log.lock().unwrap();
        assert_eq!(log.recovers, 1);
        assert_eq!(log.exchanges, 1);
    }
    // The following boundary re-enters successfully and exchanges.
    cyclic.exchange(Tick(3)).unwrap();
    let log = rig.log.lock().unwrap();
    assert_eq!(log.recovers, 2);
    assert_eq!(log.exchanges, 2);
    assert_eq!(driver.read(PointId(10)).unwrap().value, Value::Bool(true));
}

/// A two-station rig helper: `st0` (8in/2out, matching [`identity`])
/// plus `st1` (4in/1out, product 951) — one station per model device.
fn two_station_rig(script: Vec<FakeCycle>) -> Rig {
    let stations = vec![
        discovered()[0].clone(),
        DiscoveredStation {
            position: 1,
            name: "st1".to_string(),
            vendor_id: 0xad,
            product_id: 951,
            revision: 1,
            input_bytes: 4,
            output_bytes: 1,
        },
    ];
    rig_fakes(stations, script, |_| {})
}

/// A one-input-channel device declaration: `channel` at its station's
/// input image bit 0, bound to the `product`/`revision` identity —
/// each device's offsets are station-relative.
fn di_parameters(
    product: u32,
    revision: u32,
    channel: &str,
) -> BTreeMap<String, serde_json::Value> {
    serde_json::from_value(json!({
        "bus": "b0",
        "identity": {"vendor": 0xad, "product": product, "revision": revision},
        "mapping": {
            "inputs": {channel: {"byte": 0, "bit": 0}}
        },
        "exchange_miss_threshold": 2,
        "startup": {"on_mismatch": "fail"}
    }))
    .unwrap()
}

fn di_only(channel: &str, point: u64) -> (BTreeMap<String, ChannelDecl>, Vec<BusPoint>) {
    (
        [(
            channel.to_string(),
            ChannelDecl {
                direction: Direction::In,
                kind: ValueKind::Bool,
            },
        )]
        .into_iter()
        .collect(),
        vec![BusPoint {
            point: PointId(point),
            channel: channel.to_string(),
            direction: Direction::In,
            kind: ValueKind::Bool,
        }],
    )
}

#[test]
fn a_short_exchange_degrades_only_the_named_station() {
    // Two devices, one per station: a shortfall on station 1 must not
    // escalate station 0's points.
    let rig = two_station_rig(vec![
        FakeCycle::short(Some(1), vec![0b1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
        FakeCycle::complete(vec![0b1, 0, 0, 0, 0, 0, 0, 0, 0b1, 0, 0, 0]),
    ]);
    let (channels_one, points_one) = di_only("di-1", 10);
    let (channels_two, points_two) = di_only("di-2", 20);
    let first = rig
        .attach(
            1,
            &di_parameters(950, 2, "di-1"),
            &channels_one,
            &points_one,
        )
        .unwrap();
    let second = rig
        .attach(
            2,
            &di_parameters(951, 1, "di-2"),
            &channels_two,
            &points_two,
        )
        .unwrap();
    let cyclic = first.cyclic().unwrap();
    cyclic.exchange(Tick(1)).unwrap();
    // Station 0 latched at the tick; station 1's point escalates.
    let kept = first.read(PointId(10)).unwrap();
    assert_eq!(kept.value, Value::Bool(true));
    assert_eq!(kept.tick, Tick(1));
    assert_eq!(
        second.read(PointId(20)),
        Err(IoError::Disconnected(PointId(20)))
    );
    // A clean exchange clears the attribution.
    cyclic.exchange(Tick(2)).unwrap();
    assert_eq!(second.read(PointId(20)).unwrap().value, Value::Bool(true));
}

#[test]
fn an_unattributable_shortfall_degrades_the_whole_bus() {
    let rig = rig(vec![
        FakeCycle::short(None, input_image(true, 0.0)),
        FakeCycle::complete(input_image(true, 0.0)),
    ]);
    let driver = rig.driver(1);
    let cyclic = driver.cyclic().unwrap();
    cyclic.exchange(Tick(1)).unwrap();
    assert_eq!(
        driver.read(PointId(10)),
        Err(IoError::Disconnected(PointId(10)))
    );
    cyclic.exchange(Tick(2)).unwrap();
    assert_eq!(driver.read(PointId(10)).unwrap().value, Value::Bool(true));
}

#[test]
fn late_exchanges_count_as_missed_deadlines() {
    let rig = rig(vec![
        FakeCycle::late(input_image(true, 0.0)),
        FakeCycle::complete(input_image(true, 0.0)),
    ]);
    let driver = rig.driver(1);
    let cyclic = driver.cyclic().unwrap();
    cyclic.exchange(Tick(1)).unwrap();
    let diagnostics = driver.diagnostics().unwrap();
    let exchange = diagnostics.exchange.unwrap();
    assert_eq!(exchange.missed_deadlines, 1);
    assert_eq!(exchange.succeeded, 1);
}

#[test]
fn diagnostics_reports_link_state_and_exchange_counters() {
    let rig = rig(vec![
        FakeCycle::complete(input_image(true, 0.0)),
        FakeCycle::failed(TransportError::disconnected("link flap")),
        FakeCycle::short(Some(0), input_image(false, 0.0)),
        FakeCycle::complete(input_image(true, 0.0)),
    ]);
    let driver = rig.driver(1);
    let cyclic = driver.cyclic().unwrap();
    cyclic.exchange(Tick(1)).unwrap();
    let diagnostics = driver.diagnostics().unwrap();
    assert_eq!(diagnostics.link, LinkState::Connected);
    assert_eq!(diagnostics.last_error, None);
    cyclic.exchange(Tick(2)).unwrap_err();
    let diagnostics = driver.diagnostics().unwrap();
    assert_eq!(diagnostics.link, LinkState::Disconnected);
    assert!(diagnostics.last_error.unwrap().contains("link flap"));
    // The shortfall recovers the link but records the mismatch.
    cyclic.exchange(Tick(3)).unwrap();
    cyclic.exchange(Tick(4)).unwrap();
    let diagnostics = driver.diagnostics().unwrap();
    assert_eq!(diagnostics.link, LinkState::Connected);
    let exchange = diagnostics.exchange.unwrap();
    assert_eq!(exchange.attempted, 4);
    assert_eq!(exchange.succeeded, 3);
    assert_eq!(exchange.working_counter_mismatches, 1);
    assert_eq!(exchange.last_exchange_tick, Some(Tick(4)));
}

#[test]
fn kind_and_point_checks_happen_locally() {
    let rig = rig(vec![]);
    let driver = rig.driver(1);
    // Unknown points are refused by name.
    assert_eq!(
        driver.read(PointId(999)),
        Err(IoError::UnknownPoint(PointId(999)))
    );
    assert_eq!(
        driver.write(PointId(999), Value::Bool(true)),
        Err(IoError::UnknownPoint(PointId(999)))
    );
    // Input points have no writable surface.
    assert_eq!(
        driver.write(PointId(10), Value::Bool(true)),
        Err(IoError::UnknownPoint(PointId(10)))
    );
    // Writes are kind-checked against the channel.
    match driver.write(PointId(13), Value::Bool(true)) {
        Err(IoError::TypeMismatch { point, .. }) => assert_eq!(point, PointId(13)),
        other => panic!("expected TypeMismatch, got {other:?}"),
    }
    assert_eq!(rig.log.lock().unwrap().exchanges, 0);
}

#[test]
fn a_second_device_shares_the_bus_without_its_cyclic_surface() {
    // A second device declaring the second station's identity, mapping
    // `di-2`/`do-2` inside its own station image.
    let rig = two_station_rig(vec![FakeCycle::complete({
        let mut image = input_image(true, 1.0);
        image.extend([0b1, 0, 0, 0]);
        image
    })]);
    let parameters_two = serde_json::from_value::<BTreeMap<String, serde_json::Value>>(json!({
        "bus": "b0",
        "identity": {"vendor": 0xad, "product": 951, "revision": 1},
        "mapping": {
            "inputs": {"di-2": {"byte": 0, "bit": 0}},
            "outputs": {"do-2": {"byte": 0, "bit": 0}}
        },
        "exchange_miss_threshold": 2,
        "safe_outputs": {"do-2": {"bool": false}},
        "startup": {"on_mismatch": "fail"}
    }))
    .unwrap();
    let channels_two: BTreeMap<String, ChannelDecl> = [
        (
            "di-2",
            ChannelDecl {
                direction: Direction::In,
                kind: ValueKind::Bool,
            },
        ),
        (
            "do-2",
            ChannelDecl {
                direction: Direction::Out,
                kind: ValueKind::Bool,
            },
        ),
    ]
    .into_iter()
    .map(|(name, decl)| (name.to_string(), decl))
    .collect();
    let points_two = vec![
        BusPoint {
            point: PointId(20),
            channel: "di-2".to_string(),
            direction: Direction::In,
            kind: ValueKind::Bool,
        },
        BusPoint {
            point: PointId(21),
            channel: "do-2".to_string(),
            direction: Direction::Out,
            kind: ValueKind::Bool,
        },
    ];
    let first = rig.driver(1);
    let second = rig
        .attach(2, &parameters_two, &channels_two, &points_two)
        .unwrap();
    // The bus opened once; only the first attacher carries the cyclic
    // surface and diagnostics.
    assert_eq!(rig.opens.load(Ordering::SeqCst), 1);
    assert!(first.cyclic().is_some());
    assert!(second.cyclic().is_none());
    assert!(first.diagnostics().is_some());
    assert!(second.diagnostics().is_none());
    // The owner's one exchange latches both devices' inputs.
    first.cyclic().unwrap().exchange(Tick(9)).unwrap();
    let log = rig.log.lock().unwrap();
    assert_eq!(log.exchanges, 1);
    drop(log);
    let held = second.read(PointId(20)).unwrap();
    assert_eq!(held.tick, Tick(9));
    assert_eq!(held.value, Value::Bool(true));
}

#[test]
fn a_second_device_claiming_the_same_station_is_refused() {
    // Identical identities bind in attach order — the second device
    // finds no unclaimed matching station.
    let rig = rig(vec![]);
    rig.driver(1);
    let error = rig.device(2).err().unwrap();
    assert!(matches!(error, AttachError::Backend(_)), "{error:?}");
    assert!(error.to_string().contains("unclaimed"), "{error}");
}
