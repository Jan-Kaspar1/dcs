//! The recorded-link replay tests (`#550`): the checked-in captures
//! under `tests/captures/` driven against `dcs-ethercat`'s transport and
//! master seam, asserting the documented per-outcome counters, held-image
//! aging, threshold escalation, and recovery re-entry.
//!
//! See `tests/README.md` for the pattern and how to record a new
//! capture.

mod support;

use dcs_core::{Direction, IoDriver, IoError, LinkState, PointId, Tick, Value, ValueKind};
use dcs_ethercat::testing::FakeTransport;
use dcs_ethercat::{
    AttachError, BusPoint, BusTransport, Capture, ChannelDecl, DeviceParameters, DiscoveredStation,
    EthercatBuses, TransportError,
};
use dcs_model::DeviceId;
use serde_json::json;
use std::collections::BTreeMap;
use std::sync::Arc;

use support::{Rig, channels, load_capture, parameters, points};

/// The capture directory's two checked-in recordings, in a stable
/// order — the replay suite's own case list.
const CAPTURES: [&str; 2] = ["wago-750-354-known-good", "wago-750-354-link-flap"];

/// Every checked-in capture parses and is internally consistent: a
/// broken capture fails here by name rather than as a puzzling case
/// failure later.
#[test]
fn every_checked_in_capture_parses() {
    for name in CAPTURES {
        let capture = load_capture(name);
        assert!(!capture.cycles.is_empty(), "{name} records no cycles");
        assert!(!capture.stations.is_empty(), "{name} discovered nothing");
        assert_eq!(capture.name, name, "the capture names itself");
    }
}

#[test]
fn a_replayed_known_good_capture_latches_every_completed_exchange() {
    // The known-good recording: four complete exchanges, one late frame,
    // one station-0 shortfall, one recovery. Each completed exchange
    // latches the returned image at its own scan tick, and the two
    // digital input channels read the bits the coupler recorded.
    let capture = load_capture("wago-750-354-known-good");
    let rig = Rig::over(&capture, "b0");
    let driver = rig.driver(1);
    let cyclic = driver.cyclic().expect("the bus owner is cyclic");

    // The coupler's recorded input bit 0 walks false, true, true, false
    // across the four complete exchanges.
    for (index, (tick, di_bit)) in [
        (Tick(1), false),
        (Tick(2), true),
        (Tick(3), true),
        (Tick(4), false),
    ]
    .into_iter()
    .enumerate()
    {
        cyclic.exchange(tick).unwrap();
        let di = driver.read(PointId(10)).unwrap();
        assert_eq!(di.tick, tick, "cycle {index} latched at its own tick");
        assert_eq!(di.value, Value::Bool(di_bit));
    }

    // The late frame still completed: its image latched, and it counts a
    // missed deadline alongside the completed exchange.
    cyclic.exchange(Tick(5)).unwrap();
    assert_eq!(driver.read(PointId(10)).unwrap().tick, Tick(5));
    let diagnostics = driver.diagnostics().unwrap();
    let exchange = diagnostics.exchange.unwrap();
    assert_eq!(exchange.attempted, 5);
    assert_eq!(exchange.succeeded, 5);
    assert_eq!(
        exchange.missed_deadlines, 1,
        "the late frame is the deadline record"
    );
    assert_eq!(exchange.working_counter_mismatches, 0);

    // The station-0 shortfall is attributed: the bus answered, the
    // station's points escalate, and only that station's mismatch counts.
    cyclic.exchange(Tick(6)).unwrap();
    let exchange = driver.diagnostics().unwrap().exchange.unwrap();
    assert_eq!(exchange.working_counter_mismatches, 1);
    assert_eq!(
        driver.read(PointId(10)),
        Err(IoError::Disconnected(PointId(10))),
        "the shortfall names station 0, whose points degrade"
    );

    // The recovery exchange re-enters at the boundary: the mismatch
    // clears and the recording's final image latches — visibly fresh,
    // not the held value the shortfall left standing.
    cyclic.exchange(Tick(7)).unwrap();
    let fresh = driver.read(PointId(10)).unwrap();
    assert_eq!(fresh.value, Value::Bool(false));
    assert_eq!(fresh.tick, Tick(7));
    assert_eq!(driver.read(PointId(11)).unwrap().value, Value::Int(17));
    let exchange = driver.diagnostics().unwrap().exchange.unwrap();
    assert_eq!(
        exchange.working_counter_mismatches, 1,
        "the count is cumulative"
    );
    assert_eq!(exchange.attempted, 7);
    assert_eq!(exchange.succeeded, 7);
}

#[test]
fn a_replayed_link_flap_ages_the_held_image_and_escalates_at_the_threshold() {
    // The link-flap recording: three complete exchanges, then three that
    // never complete. Below the declared miss threshold of 2 the held
    // image still serves — aged to its own acquisition stamp, never a
    // silent zero — and at the threshold every read escalates. The
    // recording's clean tail recovers at the next boundary.
    let capture = load_capture("wago-750-354-link-flap");
    let rig = Rig::over(&capture, "b0");
    let driver = rig.driver(1);
    let cyclic = driver.cyclic().expect("the bus owner is cyclic");

    cyclic.exchange(Tick(1)).unwrap();
    cyclic.exchange(Tick(2)).unwrap();
    cyclic.exchange(Tick(3)).unwrap();
    driver.write(PointId(12), Value::Bool(true)).unwrap();
    assert_eq!(
        driver.read(PointId(10)).unwrap().tick,
        Tick(3),
        "the last completed exchange's acquisition stamp"
    );

    // First dropped frame: the exchange did not complete and the staged
    // image is retained — the read still serves the latched value.
    assert_eq!(
        cyclic.exchange(Tick(4)),
        Err(IoError::Disconnected(PointId(10)))
    );
    let held = driver.read(PointId(10)).unwrap();
    assert_eq!(
        held.value,
        Value::Bool(true),
        "the held image answers, unaltered"
    );
    assert_eq!(held.tick, Tick(3), "aged to its own acquisition stamp");
    assert_eq!(
        driver.read(PointId(12)).unwrap().value,
        Value::Bool(true),
        "a failed exchange retains the staged image for the next publication"
    );

    // Second dropped frame reaches the declared threshold of 2.
    assert_eq!(
        cyclic.exchange(Tick(5)),
        Err(IoError::Disconnected(PointId(10)))
    );
    assert_eq!(
        driver.read(PointId(10)),
        Err(IoError::Disconnected(PointId(10))),
        "the threshold escalates reads to Disconnected rather than serving a stale value"
    );

    // Third dropped frame: the streak is the driver's own.
    assert_eq!(
        cyclic.exchange(Tick(6)),
        Err(IoError::Disconnected(PointId(10)))
    );
    let diagnostics = driver.diagnostics().unwrap();
    assert_eq!(diagnostics.link, LinkState::Disconnected);
    assert!(
        diagnostics
            .last_error
            .as_deref()
            .is_some_and(|error| error.contains("link down")),
        "{diagnostics:?}"
    );
    let exchange = diagnostics.exchange.unwrap();
    assert_eq!(exchange.attempted, 6);
    assert_eq!(exchange.succeeded, 3);

    // The recorded recovery: the first clean boundary re-enters the
    // cyclic state path, clears the miss streak, and latches fresh.
    cyclic.exchange(Tick(7)).unwrap();
    let fresh = driver.read(PointId(10)).unwrap();
    assert_eq!(
        fresh.value,
        Value::Bool(false),
        "the coupler answered again, with a value the outage never carried"
    );
    assert_eq!(fresh.tick, Tick(7));
    let diagnostics = driver.diagnostics().unwrap();
    assert_eq!(diagnostics.link, LinkState::Connected);
    let exchange = diagnostics.exchange.unwrap();
    assert_eq!(exchange.succeeded, 4);
    assert_eq!(
        exchange.attempted - exchange.succeeded,
        3,
        "the recording's three dropped frames are the exchange record"
    );
}

#[test]
fn every_replayed_capture_is_deterministic() {
    // The pattern's whole point: a recorded run replays identically on
    // any host with no NIC. Two passes over the same capture produce the
    // same per-exchange outcome sequence, the same latched values, and
    // the same diagnostics counters.
    for name in CAPTURES {
        let run = || {
            let capture = load_capture(name);
            let rig = Rig::over(&capture, "b0");
            let driver = rig.driver(1);
            let cyclic = driver.cyclic().unwrap();
            let mut observations = Vec::new();
            for tick in 1..=10u64 {
                let outcome = cyclic
                    .exchange(Tick(tick))
                    .map_err(|error| error.to_string());
                observations.push((
                    format!("{outcome:?}"),
                    driver
                        .read(PointId(10))
                        .map(|sample| format!("{:?}", sample.value)),
                ));
            }
            let diagnostics = driver.diagnostics().unwrap();
            (observations, diagnostics.exchange)
        };
        assert_eq!(run(), run(), "{name} does not replay deterministically");
    }
}

#[test]
fn a_replayed_capture_serves_a_second_device_on_the_same_bus() {
    // The capture is the whole bus: a second model device declaring its
    // own station on the recorded logical bus attaches to the same
    // master and shares the replayed exchange — the recorded layout is
    // verified per device before outputs enable.
    let capture = load_capture("wago-750-354-known-good");
    let mut stations = capture.discovered();
    stations.push(DiscoveredStation {
        position: 1,
        name: "WAGO 750-354".to_string(),
        vendor_id: 6,
        product_id: 354,
        revision: 3,
        input_bytes: 2,
        output_bytes: 2,
    });
    let replay = Capture {
        name: capture.name.clone(),
        stations,
        cycles: capture.cycles.clone(),
    };
    let rig = Rig::over(&replay, "b0");
    let first = rig.driver(1);
    let second = rig
        .attach(
            2,
            &second_device_parameters("b0"),
            &second_device_channels(),
            &second_device_points(),
        )
        .unwrap();
    // One exchange moves the whole bus's image: both devices' points come
    // off the one recorded frame, each from its own station's bytes.
    first.cyclic().unwrap().exchange(Tick(1)).unwrap();
    let exchange = first.diagnostics().unwrap().exchange.unwrap();
    assert_eq!(exchange.attempted, 1, "one exchange covers both stations");
    assert_eq!(exchange.succeeded, 1);
    // The recording returns bytes only for the first station's two-byte
    // input area, so the second station's bytes held zero and latched as
    // such — from the same frame, not from a second exchange.
    assert_eq!(second.read(PointId(20)).unwrap().tick, Tick(1));
    assert_eq!(second.read(PointId(21)).unwrap().value, Value::Int(0));
    // The second device's write joins the one staged image, and the first
    // device's exchange publishes it — no second exchange ran for it.
    second.write(PointId(22), Value::Bool(true)).unwrap();
    assert_eq!(
        first.read(PointId(12)).unwrap().value,
        Value::Bool(true),
        "the safe state seeded the first station's output byte"
    );
    first.cyclic().unwrap().exchange(Tick(2)).unwrap();
    assert_eq!(
        second.read(PointId(22)).unwrap().value,
        Value::Bool(true),
        "the staged write published on the bus's one exchange"
    );
    assert_eq!(
        first.diagnostics().unwrap().exchange.unwrap().attempted,
        2,
        "the bus ran one exchange per call, not one per device"
    );
}

/// A second model device on the same recorded bus: its own station at
/// bus position 1, its channels mapped onto that station's image bytes.
fn second_device_parameters(bus: &str) -> BTreeMap<String, serde_json::Value> {
    serde_json::from_value(json!({
        "bus": bus,
        "identity": {"vendor": 6, "product": 354, "revision": 3},
        "mapping": {
            "inputs": {"di-2": {"byte": 0, "bit": 0}, "ai-2": {"byte": 1, "bits": 8}},
            "outputs": {"do-2": {"byte": 0, "bit": 0}, "ao-2": {"byte": 1, "bits": 8}}
        },
        "exchange_miss_threshold": 2,
        "safe_outputs": {"do-2": {"bool": false}, "ao-2": {"int": 2}},
        "startup": {"on_mismatch": "fail"}
    }))
    .unwrap()
}

/// The second device's channel map.
fn second_device_channels() -> BTreeMap<String, ChannelDecl> {
    [
        ("di-2", Direction::In, ValueKind::Bool),
        ("ai-2", Direction::In, ValueKind::Int),
        ("do-2", Direction::Out, ValueKind::Bool),
        ("ao-2", Direction::Out, ValueKind::Int),
    ]
    .into_iter()
    .map(|(name, direction, kind)| (name.to_string(), ChannelDecl { direction, kind }))
    .collect()
}

/// The second device's bus points.
fn second_device_points() -> Vec<BusPoint> {
    [
        (20, "di-2", Direction::In, ValueKind::Bool),
        (21, "ai-2", Direction::In, ValueKind::Int),
        (22, "do-2", Direction::Out, ValueKind::Bool),
        (23, "ao-2", Direction::Out, ValueKind::Int),
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

#[test]
fn a_capture_that_does_not_match_the_declared_identity_fails_before_op() {
    // A capture is verified exactly like a live bus: a recording whose
    // station identity differs from the model's declared identity fails
    // startup before any exchange, and the run never serves the
    // mismatched image.
    let capture = load_capture("wago-750-354-known-good");
    let rig = Rig::over(&capture, "b0");
    let mut declared = parameters("b0");
    declared.insert(
        "identity".to_string(),
        json!({"vendor": 6, "product": 354, "revision": 99}),
    );
    let error = rig
        .attach(1, &declared, &channels(), &points())
        .err()
        .unwrap();
    assert!(matches!(error, AttachError::Backend(_)), "{error:?}");
    assert!(error.to_string().contains("revision"), "{error}");
    assert_eq!(
        rig.driver(1)
            .diagnostics()
            .unwrap()
            .exchange
            .unwrap()
            .attempted,
        0
    );
}

#[test]
fn a_capture_whose_layout_differs_from_the_mapping_fails_before_op() {
    let capture = load_capture("wago-750-354-known-good");
    let rig = Rig::over(&capture, "b0");
    let mut declared = parameters("b0");
    declared.insert(
        "identity".to_string(),
        json!({"vendor": 6, "product": 354, "revision": 3}),
    );
    // A channel mapped past the recorded two-byte input area.
    declared.insert(
        "mapping".to_string(),
        json!({
            "inputs": {"di-1": {"byte": 0, "bit": 0}, "ai-1": {"byte": 4, "bits": 32}},
            "outputs": {"do-1": {"byte": 0, "bit": 0}, "ao-1": {"byte": 1, "bits": 8}}
        }),
    );
    let error = rig
        .attach(1, &declared, &channels(), &points())
        .err()
        .unwrap();
    assert!(matches!(error, AttachError::Backend(_)), "{error:?}");
    assert!(error.to_string().contains("input area"), "{error}");
}

#[test]
fn a_missing_capture_binding_is_a_named_failure_not_a_simulated_bus() {
    // The deployment seam: a logical bus with no binding never opens, and
    // an unknown station never substitutes simulation. This is the same
    // refusal a live binding produces, so the recorded path cannot mask
    // a deployment mistake.
    let capture = load_capture("wago-750-354-known-good");
    let buses = EthercatBuses::with_opener(
        BTreeMap::from([("other".to_string(), "eth0".to_string())]),
        capture.opener(),
    );
    let parsed = DeviceParameters::parse(&parameters("b0"), &channels()).unwrap();
    let error = buses.attach(DeviceId(1), &parsed, &points()).err().unwrap();
    assert!(
        error.to_string().contains("no interface binding"),
        "{error}"
    );
}

#[test]
fn the_scripted_fake_and_the_capture_prove_the_same_contract() {
    // The replay pattern's regression base: the scripted fake's
    // known-good exchange and the capture's own produce identical
    // observable outcomes, so a capture proves the same contract rather
    // than a different one.
    let scripted = {
        let stations = vec![DiscoveredStation {
            position: 0,
            name: "WAGO 750-354".to_string(),
            vendor_id: 6,
            product_id: 354,
            revision: 3,
            input_bytes: 2,
            output_bytes: 2,
        }];
        let buses = EthercatBuses::with_opener(
            BTreeMap::from([("b0".to_string(), "eth0".to_string())]),
            Arc::new(move |_request| {
                Ok(Box::new(FakeTransport::with_script(
                    stations.clone(),
                    vec![dcs_ethercat::testing::FakeCycle::complete(vec![0x00, 0x00])],
                )))
            }),
        );
        let mut parameters = parameters("b0");
        parameters.insert(
            "identity".to_string(),
            json!({"vendor": 6, "product": 354, "revision": 3}),
        );
        let declared = channels();
        let driver = Arc::new(
            buses
                .attach(
                    DeviceId(1),
                    &DeviceParameters::parse(&parameters, &declared).unwrap(),
                    &points(),
                )
                .unwrap(),
        );
        driver.cyclic().unwrap().exchange(Tick(1)).unwrap();
        driver.read(PointId(10)).unwrap()
    };

    let capture = load_capture("wago-750-354-known-good");
    let replayed = Rig::over(&capture, "b0");
    let driver = replayed.driver(1);
    driver.cyclic().unwrap().exchange(Tick(1)).unwrap();
    let sample = driver.read(PointId(10)).unwrap();
    assert_eq!(sample.value, scripted.value);
    assert_eq!(sample.tick, scripted.tick);
}

/// The capture's own transport answers from the recording alone — the
/// seam the master sits on, driven without any model.
#[test]
fn a_capture_replays_over_the_transport_seam_directly() {
    let capture = load_capture("wago-750-354-link-flap");
    let mut transport = capture.transport();
    let mut inputs = [0u8; 2];
    let mut outcomes = Vec::new();
    for _ in 0..8 {
        outcomes.push(transport.exchange(&[0, 0], &mut inputs));
    }
    assert_eq!(
        outcomes,
        vec![
            Ok(dcs_ethercat::CycleOutcome::Complete),
            Ok(dcs_ethercat::CycleOutcome::Complete),
            Ok(dcs_ethercat::CycleOutcome::Complete),
            Err(TransportError::Disconnected(
                "link down: no frame returned within 10ms".to_string()
            )),
            Err(TransportError::Disconnected(
                "link down: no frame returned within 10ms".to_string()
            )),
            Err(TransportError::Disconnected(
                "link down: no frame returned within 10ms".to_string()
            )),
            Ok(dcs_ethercat::CycleOutcome::Complete),
            Ok(dcs_ethercat::CycleOutcome::Complete),
        ],
        "the recording answers in its recorded order"
    );
    assert_eq!(transport.served(), 8);
    assert_eq!(inputs, [0x00, 0x11], "the recording's last returned image");
}
