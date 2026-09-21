//! Scratch reproduction for issue #686 — demote/promote cycles
//! re-journaling settled command receipts.

use dcs_core::{
    Command, CommandOutcome, Direction, IoDriver, IoError, JournalEvent, PointId, Role, Sample,
    StandbySync, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Driven, Monitor, MonitorClient};
use dcs_runtime::{Executor, Peer, PointMap};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Mutex;
use std::thread::{self, JoinHandle};

struct StubDriver {
    points: Mutex<HashMap<PointId, Sample>>,
}

impl StubDriver {
    fn new(points: &[(PointId, Value)]) -> Self {
        Self {
            points: Mutex::new(
                points
                    .iter()
                    .map(|&(point, value)| (point, Sample::good(value, Tick::ZERO)))
                    .collect(),
            ),
        }
    }
}

impl IoDriver for StubDriver {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        self.points
            .lock()
            .unwrap()
            .get(&point)
            .copied()
            .ok_or(IoError::UnknownPoint(point))
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        let mut points = self.points.lock().unwrap();
        let sample = points.get_mut(&point).ok_or(IoError::UnknownPoint(point))?;
        *sample = Sample::good(value, Tick::ZERO);
        Ok(())
    }
}

const MODEL: &str = include_str!("../fixtures/monitor.json");

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

const RECEIPT_CAPACITY: usize = 6;

fn executor(driver: &'static StubDriver) -> Executor<'static> {
    Executor::new(
        driver,
        PointMap::new().with_writable_internal(
            PointId(40),
            Direction::In,
            ValueKind::Bool,
            Value::Bool(false),
        ),
        Vec::new(),
    )
    .unwrap()
    .with_receipt_log_capacity(RECEIPT_CAPACITY)
}

struct Serving {
    monitor: std::sync::Arc<Monitor<'static>>,
    client: MonitorClient,
    thread: Option<JoinHandle<()>>,
}

impl Serving {
    fn start(monitor: Monitor<'static>) -> Self {
        let monitor = std::sync::Arc::new(monitor);
        let client = MonitorClient::new(monitor.local_addr());
        let serving = std::sync::Arc::clone(&monitor);
        Self {
            monitor,
            client,
            thread: Some(thread::spawn(move || serving.serve())),
        }
    }
}

impl Drop for Serving {
    fn drop(&mut self) {
        self.monitor.shutdown();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

fn settles(client: &MonitorClient) -> Vec<(u64, CommandOutcome)> {
    client
        .journal(0)
        .unwrap()
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::CommandSettled { receipt } => {
                Some((entry.tick.0, receipt.outcome.clone()))
            }
            _ => None,
        })
        .collect()
}

#[test]
fn repro() {
    let driver_a: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[])));
    let a = Serving::start(
        Monitor::bind_peer(
            "127.0.0.1:0",
            Peer::active(executor(driver_a), None),
            signal_index(),
        )
        .unwrap(),
    );
    let addr_a = a.monitor.local_addr();

    let driver_b: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[])));
    let b = Serving::start(
        Monitor::bind_peer(
            "127.0.0.1:0",
            Peer::standby(executor(driver_b), None),
            signal_index(),
        )
        .unwrap()
        .driven(Driven {
            track: Some(addr_a),
            after_scan: None,
        }),
    );
    let addr_b = b.monitor.local_addr();

    a.client.advance(3).unwrap();
    b.client.advance(1).unwrap();
    assert!(matches!(
        b.client.role().unwrap().sync,
        Some(StandbySync::Tracking { .. })
    ));

    // Fill the receipt log to capacity with settled commands so the
    // bounded window's base is in play.
    for i in 0..RECEIPT_CAPACITY {
        let command = Command::WriteValue {
            point: PointId(40),
            kind: ValueKind::Bool,
            value: Value::Bool(i % 2 == 0),
        };
        a.client.command(&command).unwrap();
        a.client.advance(1).unwrap();
        b.client.advance(1).unwrap();
    }
    eprintln!("A receipts len: {}", a.client.receipts().unwrap().len());
    eprintln!("B receipts len: {}", b.client.receipts().unwrap().len());

    let mut a_active = true;
    for cycle in 0..10 {
        let (owner, other) = if a_active { (&a, &b) } else { (&b, &a) };
        // Race a command against the demote: accepted, then the owner
        // demotes before it can apply — suspended in the log.
        let command = Command::WriteValue {
            point: PointId(40),
            kind: ValueKind::Bool,
            value: Value::Bool(true),
        };
        let receipt = owner.client.command(&command).unwrap();
        owner.client.demote().unwrap();
        other.client.promote().unwrap();
        // Let the promoted peer settle and the demoted peer reconverge.
        for _ in 0..3 {
            other.client.advance(1).unwrap();
            owner.client.advance(1).unwrap();
        }
        let sa = settles(&owner.client);
        let sb = settles(&other.client);
        eprintln!(
            "cycle {cycle}: receipt {:?} | ex-owner settles {} | promoted settles {}",
            receipt.outcome,
            sa.len(),
            sb.len()
        );
        eprintln!(
            "  ex-owner receipts {} base? | promoted receipts {}",
            owner.client.receipts().unwrap().len(),
            other.client.receipts().unwrap().len()
        );
        a_active = !a_active;
    }
    eprintln!("A journal settles: {:?}", settles(&a.client));
    eprintln!("B journal settles: {:?}", settles(&b.client));
    eprintln!("A receipts: {:?}", a.client.receipts().unwrap());
    eprintln!("B receipts: {:?}", b.client.receipts().unwrap());
    let _ = (addr_a, addr_b);
}
