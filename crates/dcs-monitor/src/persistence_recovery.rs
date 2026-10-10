//! Deterministic injected storage failures at the real HTTP/scan boundary.

use crate::drain::Drain;
use crate::{Monitor, MonitorClient, MonitorConfig, StateSink};
use dcs_core::{
    Command, CommandOutcome, Direction, IoDriver, IoError, JournalEvent, PointId, Role, Sample,
    Tick, Value, ValueKind,
};
use dcs_model::SignalIndex;
use dcs_runtime::{Executor, Peer, PointMap, PointSpec, WriteGate};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

type WriterGate = Arc<(Mutex<bool>, Condvar)>;

struct Driver(Mutex<Sample>);

impl IoDriver for Driver {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        if point != PointId(10) {
            return Err(IoError::UnknownPoint(point));
        }
        Ok(*self.0.lock().unwrap())
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        if point != PointId(10) {
            return Err(IoError::UnknownPoint(point));
        }
        *self.0.lock().unwrap() = Sample::good(value, Tick::ZERO);
        Ok(())
    }
}

fn fixture(
    name: &str,
) -> (
    Monitor<'static>,
    &'static WriteGate<'static>,
    Arc<AtomicBool>,
    PathBuf,
) {
    let driver = Box::leak(Box::new(Driver(Mutex::new(Sample::good(
        Value::Float(0.0),
        Tick::ZERO,
    )))));
    let gate = Box::leak(Box::new(WriteGate::closed(driver)));
    let map = PointMap::new().with_spec(
        PointId(10),
        PointSpec {
            direction: Direction::In,
            kind: ValueKind::Float,
            internal: None,
            writable: true,
            requires_reason: false,
            stale_after_ticks: None,
            journaled: true,
            record_every_ticks: Some(1),
        },
    );
    let executor = Executor::new(gate, map, Vec::new())
        .unwrap()
        .with_submission_origin(123456);
    let released = Arc::new(AtomicBool::new(false));
    let peer = Peer::active(executor, Some(gate)).with_field_release({
        let released = Arc::clone(&released);
        move || {
            released.store(true, Ordering::SeqCst);
        }
    });
    let monitor = Monitor::bind_peer_with(
        "127.0.0.1:0",
        peer,
        SignalIndex {
            points: Vec::new(),
            components: Vec::new(),
            equipment: Vec::new(),
            views: Vec::new(),
        },
        MonitorConfig::default(),
    )
    .unwrap();
    monitor.activate().unwrap();
    let dir = std::env::temp_dir().join(format!("dcs-recovery-{name}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    (monitor, gate, released, dir)
}

/// A writer's first call parks until the test opens it. The producing
/// side is untouched; saturation occurs at exactly the declared bound.
fn stalled<T: Send + 'static>(label: &str) -> (Drain<T>, WriterGate, Arc<AtomicBool>) {
    let gate = Arc::new((Mutex::new(false), Condvar::new()));
    let entered = Arc::new(AtomicBool::new(false));
    let drain = Drain::new(label.to_string(), 1, {
        let gate = Arc::clone(&gate);
        let entered = Arc::clone(&entered);
        move |_record| {
            entered.store(true, Ordering::SeqCst);
            let (lock, signal) = &*gate;
            let mut open = lock.lock().unwrap();
            while !*open {
                open = signal.wait(open).unwrap();
            }
            Ok(())
        }
    });
    (drain, gate, entered)
}

fn parked(entered: &AtomicBool) {
    let deadline = Instant::now() + Duration::from_secs(2);
    while !entered.load(Ordering::SeqCst) {
        assert!(Instant::now() < deadline, "writer did not park");
        std::thread::sleep(Duration::from_millis(1));
    }
}

fn release(gate: &WriterGate) {
    *gate.0.lock().unwrap() = true;
    gate.1.notify_all();
}

/// Release injected writers and serving lanes even if an assertion fails.
/// This guard must live inside the scope so it drops before worker joins.
struct Teardown<'a> {
    monitor: &'a Monitor<'static>,
    gate: &'a WriterGate,
}

impl Drop for Teardown<'_> {
    fn drop(&mut self) {
        release(self.gate);
        self.monitor.shutdown();
    }
}

#[test]
fn every_saturated_sink_fences_and_answers_without_poisoning_the_monitor() {
    for family in ["state", "journal", "history"] {
        let (mut monitor, field_gate, released, dir) = fixture(family);
        let (writer_gate, entered) = match family {
            "state" => {
                let (drain, gate, entered) = stalled("state file injected");
                monitor.with_state_sink(
                    StateSink::for_test(
                        "state file injected".to_string(),
                        &dir.join("state.json"),
                        drain,
                    )
                    .unwrap(),
                );
                monitor.persist_state().unwrap();
                parked(&entered);
                monitor.persist_state().unwrap();
                (gate, entered)
            }
            "journal" => {
                let (drain, gate, entered) = stalled("journal file injected");
                let mut shared = monitor.shared.lock().unwrap();
                shared.recorder.with_sink(drain);
                let event = || JournalEvent::PointChanged {
                    point: PointId(10),
                    from: None,
                    to: Value::Float(0.0),
                };
                shared.recorder.push(Tick::ZERO, event());
                parked(&entered);
                shared.recorder.push(Tick::ZERO, event());
                (gate, entered)
            }
            "history" => {
                let (drain, gate, entered) = stalled("history file injected");
                monitor
                    .shared
                    .lock()
                    .unwrap()
                    .recorder
                    .with_history_sink(drain);
                monitor.paced_scan();
                parked(&entered);
                monitor.paced_scan();
                (gate, entered)
            }
            _ => unreachable!(),
        };
        assert!(entered.load(Ordering::SeqCst));
        assert!(field_gate.is_open());
        let before = monitor.tick();
        std::thread::scope(|scope| {
            scope.spawn(|| monitor.serve());
            let _teardown = Teardown {
                monitor: &monitor,
                gate: &writer_gate,
            };
            let client = MonitorClient::with_timeout(monitor.local_addr(), Duration::from_secs(2));
            let started = Instant::now();
            let (status, body) = client
                .request("POST", "/scan", Some(r#"{"scans":256}"#))
                .unwrap();
            assert_eq!(status, 500, "{family}: {body}");
            assert!(
                started.elapsed() < Duration::from_secs(1),
                "sink paced the scans"
            );
            let body: serde_json::Value = serde_json::from_str(&body).unwrap();
            assert_eq!(body["scans_completed"], 1);
            assert_eq!(body["durability"], "unattested");
            assert_eq!(body["retry"], false);
            assert!(body["error"].as_str().unwrap().contains(family));
            assert!(monitor.failure().unwrap().contains("bound (1)"));
            assert!(!field_gate.is_open());
            assert!(released.load(Ordering::SeqCst));
            assert_eq!(monitor.tick(), Tick(before.0 + 1));
            assert_eq!(client.role().unwrap().role, Role::Demoting);
            let (health_status, health) = client.request("GET", "/health", None).unwrap();
            assert_eq!(health_status, 503);
            assert_eq!(
                serde_json::from_str::<serde_json::Value>(&health).unwrap()["live"],
                false
            );
            // Every remaining mutation is gated; all diagnostic locks remain
            // usable. In particular a poisoned mutex would break checkpoint.
            let tick = monitor.tick();
            assert_eq!(monitor.paced_scan(), tick);
            assert_eq!(client.request("POST", "/promote", None).unwrap().0, 503);
            assert!(client.checkpoint().is_ok());
            assert!(client.receipts().is_ok());
            assert_eq!(
                client
                    .request("POST", "/scan", Some(r#"{"scans":1}"#))
                    .unwrap()
                    .0,
                500
            );
            assert_eq!(monitor.tick(), tick);
        });
        drop(monitor);
        let _ = std::fs::remove_dir_all(dir);
    }
}

#[test]
fn a_failed_command_admission_reports_its_receipt_and_cannot_apply_after_fencing() {
    let (mut monitor, field_gate, released, dir) = fixture("admission");
    let (drain, writer_gate, entered) = stalled("state file injected");
    monitor.with_state_sink(
        StateSink::for_test(
            "state file injected".to_string(),
            &dir.join("state.json"),
            drain,
        )
        .unwrap(),
    );
    monitor.persist_state().unwrap();
    parked(&entered);
    monitor.persist_state().unwrap();
    std::thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let _teardown = Teardown {
            monitor: &monitor,
            gate: &writer_gate,
        };
        let client = MonitorClient::with_timeout(monitor.local_addr(), Duration::from_secs(2));
        let command = Command::WriteValue {
            point: PointId(10),
            kind: ValueKind::Float,
            value: Value::Float(8.0),
        };
        let (status, body) = client
            .request(
                "POST",
                "/command",
                Some(&serde_json::to_string(&command).unwrap()),
            )
            .unwrap();
        assert_eq!(status, 500, "{body}");
        let body: serde_json::Value = serde_json::from_str(&body).unwrap();
        let receipt: dcs_core::CommandReceipt =
            serde_json::from_value(body["receipt"].clone()).unwrap();
        assert_eq!(receipt.command, command);
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        assert!(receipt.submission.is_some());
        assert_eq!(body["durability"], "unattested");
        assert_eq!(body["retry"], false);
        assert_eq!(client.receipts().unwrap(), vec![receipt]);
        assert!(!field_gate.is_open());
        assert!(released.load(Ordering::SeqCst));
        assert_eq!(monitor.paced_scan(), Tick::ZERO);
        assert_eq!(
            field_gate.read(PointId(10)).unwrap().value,
            Value::Float(0.0)
        );
        let rejected = client.command(&command).unwrap();
        assert!(matches!(rejected.outcome, CommandOutcome::Rejected { .. }));
    });
    drop(monitor);
    let _ = std::fs::remove_dir_all(dir);
}
