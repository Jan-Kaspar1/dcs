//! In-process tests for the page's plant-wide overview mode: the served
//! page asset carries the overview markup, the `?pair=` parameter
//! parsing, the per-card fault-degradation rule, and the
//! link-into-pair-view logic — and the card data flow is exercised
//! against the same `GET /role` and `GET /snapshot` payloads the cards
//! poll, served by real monitor pairs.
//!
//! Each pair is two `Monitor` instances — one reporting `active`, one
//! `standby` — on their own threads, as in the pair-view tests; a
//! "dropped" peer or pair is a monitor shut down and dropped so its
//! port refuses connections.

use dcs_core::{
    Direction, IoDriver, IoError, PointId, Role, RoleReport, Sample, StandbySync,
    TelemetrySnapshot, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, Peer, PointMap, StepError,
};
use std::collections::{HashMap, HashSet};
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::thread;

/// The same minimal in-memory driver the other monitor tests use, with
/// injectable faults so a card's I/O-health input can be degraded.
struct StubDriver {
    points: Mutex<HashMap<PointId, Sample>>,
    faults: Mutex<HashSet<PointId>>,
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
            faults: Mutex::new(HashSet::new()),
        }
    }
}

impl IoDriver for StubDriver {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        if self.faults.lock().unwrap().contains(&point) {
            return Err(IoError::Disconnected(point));
        }
        self.points
            .lock()
            .unwrap()
            .get(&point)
            .copied()
            .ok_or(IoError::UnknownPoint(point))
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        if self.faults.lock().unwrap().contains(&point) {
            return Err(IoError::Disconnected(point));
        }
        let mut points = self.points.lock().unwrap();
        let sample = points.get_mut(&point).ok_or(IoError::UnknownPoint(point))?;
        if value.kind() != sample.value.kind() {
            return Err(IoError::TypeMismatch {
                point,
                expected: sample.value.kind(),
                found: value,
            });
        }
        *sample = Sample::good(value, Tick::ZERO);
        Ok(())
    }
}

/// Reads `In` point 10 and drives `Out` point 20 at gain 2 — the same
/// component shape the other monitor tests use.
struct Scale;

impl Component for Scale {
    fn name(&self) -> &str {
        "scale"
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("in", PointId(10)),
            IoRequirement::output::<f64>("out", PointId(20)),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        let sample = io.read_typed::<f64>(PointId(10))?;
        io.write_typed(PointId(20), sample.value * 2.0)?;
        Ok(())
    }
}

/// The model fixture behind the monitors; every peer serves the same index.
const MODEL: &str = include_str!("../fixtures/monitor.json");

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

/// One peer of a configured pair: its monitor served on a dedicated
/// thread. The monitor is `Arc`-shared so `stop` can drop the last
/// handle and close the listener — a simulated process outage — while
/// the leaked `'static` driver stays fault-injectable for the test.
struct PeerRig {
    monitor: Arc<Monitor<'static>>,
    client: MonitorClient,
    driver: &'static StubDriver,
    addr: SocketAddr,
    thread: Option<thread::JoinHandle<()>>,
}

impl PeerRig {
    /// Assembles a peer executor over a private stub driver and serves
    /// its monitor on a spawned thread, like the pair-view rig.
    fn start(role: Role) -> Self {
        let driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
            (PointId(10), Value::Float(0.0)),
            (PointId(20), Value::Float(0.0)),
            (PointId(30), Value::Float(0.0)),
        ])));
        let map = PointMap::new()
            .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
            .with_point(PointId(20), Direction::Out, ValueKind::Float)
            .with_point(PointId(30), Direction::Out, ValueKind::Float);
        let executor = Executor::new(driver, map, vec![Box::new(Scale)]).unwrap();
        let peer = match role {
            Role::Active => Peer::active(executor, None),
            _ => Peer::standby(executor, None),
        };
        let monitor = Arc::new(Monitor::bind_peer("127.0.0.1:0", peer, signal_index()).unwrap());
        let addr = monitor.local_addr();
        let client = MonitorClient::new(addr);
        let serving = Arc::clone(&monitor);
        let thread = thread::spawn(move || serving.serve());
        Self {
            monitor,
            client,
            driver,
            addr,
            thread: Some(thread),
        }
    }

    /// Stops serving and drops the monitor handle: the listener closes
    /// and later connects are refused — a simulated peer outage.
    fn stop(mut self) {
        self.monitor.shutdown();
        if let Some(thread) = self.thread.take() {
            thread.join().unwrap();
        }
    }
}

impl Drop for PeerRig {
    fn drop(&mut self) {
        self.monitor.shutdown();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

/// One overview card's poll, mirroring the page's `pollPairCard`: a
/// `GET /role` fetch on each of the pair's peers — a failure recorded
/// as that peer's unreachable fault — then `GET /snapshot` on the
/// selected source for the I/O-health line. Selection follows the
/// page's rule: the peer reporting settled `active`, else any
/// reachable peer. Nothing here throws: a failed peer or pair degrades
/// its own card and nothing else.
struct CardPoll {
    /// Each configured peer's role fetch — its report, or the recorded
    /// failure the card marks `unreachable` with.
    peers: Vec<Result<RoleReport, String>>,
    /// The index of the peer reporting settled `active` — the card's
    /// "active peer" identity — if one reported.
    active: Option<usize>,
    /// The selected source's snapshot, or the recorded fetch failure.
    snapshot: Result<TelemetrySnapshot, String>,
}

fn poll_card(addrs: &[SocketAddr]) -> CardPoll {
    let peers: Vec<Result<RoleReport, String>> = addrs
        .iter()
        .map(|addr| {
            MonitorClient::new(*addr)
                .role()
                .map_err(|error| error.to_string())
        })
        .collect();
    let active = peers
        .iter()
        .position(|report| matches!(report, Ok(report) if report.role == Role::Active));
    let source = active.or_else(|| peers.iter().position(|report| report.is_ok()));
    let snapshot = match source {
        Some(index) => MonitorClient::new(addrs[index])
            .snapshot()
            .map_err(|error| error.to_string()),
        None => Err("no reachable peer".to_string()),
    };
    CardPoll {
        peers,
        active,
        snapshot,
    }
}

#[test]
fn page_carries_the_overview_mode() {
    let page = dcs_monitor::PAGE;
    // The overview markup: the cards' container beside the wrapped
    // per-pair detail surface the mode hides.
    for needle in ["id=\"overview\"", "id=\"overview-cards\"", "id=\"detail\""] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
    // The pair-parameter parsing: repeated ?pair= parameters, each a
    // comma-separated host:port list with an optional name.
    for needle in [
        "getAll(\"pair\")",
        "function parsePairSpec(raw)",
        "function peerAddress(raw)",
        "spec.split(\",\")",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
    // The per-card fault-degradation rule: every card polls its pair's
    // own endpoints, records failures on the card, and a fully
    // unreachable pair still renders its named card.
    for needle in [
        "function pollPairCard(pair)",
        "pair.state[i].error = String(error)",
        "pair.snapshotError = String(error)",
        "overviewPairs.map(pollPairCard)",
        "unreachable",
        "function pairCardMarkup(pair)",
        "function selectPairSource(pair)",
        "function pairHealth(pairPeers, states)",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
    // The link into the pair's existing pair view: the same page served
    // by one of that pair's peers, the others riding as ?peer=
    // parameters — and the card reads the existing /role, /snapshot
    // payloads only: no new endpoint.
    for needle in [
        "function pairHref(pair)",
        "\"?peer=\" + others.join(\"&peer=\")",
        "\"/role\"",
        "\"/snapshot\"",
        "snapshot.io_health",
        "function ioHealthLine(pair)",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
    // The page stays a single dependency-free asset.
    assert!(!page.contains("src="), "page references external assets");
}

#[test]
fn each_pairs_card_reads_the_served_role_and_snapshot_payloads() {
    // Two configured pairs — the page's `?pair=a=…&pair=b=…` case.
    let a_active = PeerRig::start(Role::Active);
    let a_standby = PeerRig::start(Role::Standby);
    let b_active = PeerRig::start(Role::Active);
    let b_standby = PeerRig::start(Role::Standby);
    a_active.client.advance(2).unwrap();
    b_active.client.advance(1).unwrap();

    // Card A: the active peer's identity, each peer's reported role and
    // convergence, and the I/O-health line's snapshot — all from the
    // pair's existing endpoints.
    let card = poll_card(&[a_active.addr, a_standby.addr]);
    assert_eq!(card.active, Some(0));
    let report = card.peers[0].as_ref().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.sync, None);
    let report = card.peers[1].as_ref().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));
    let snapshot = card.snapshot.as_ref().unwrap();
    assert_eq!(snapshot.tick, Tick(2));
    assert_eq!(snapshot.io_health.failed_reads, 0);
    assert_eq!(snapshot.io_health.failed_writes, 0);
    assert_eq!(snapshot.io_health.last_error, None);

    // Card B is independent: its own peers' reports, its own run's tick.
    let card = poll_card(&[b_active.addr, b_standby.addr]);
    assert_eq!(card.active, Some(0));
    assert_eq!(card.snapshot.as_ref().unwrap().tick, Tick(1));

    // A field fault on A's active degrades that card's I/O-health input
    // — the executor's boundary counters are what the line reads.
    a_active.driver.faults.lock().unwrap().insert(PointId(10));
    a_active.client.advance(1).unwrap();
    let card = poll_card(&[a_active.addr, a_standby.addr]);
    let health = &card.snapshot.as_ref().unwrap().io_health;
    assert!(health.failed_reads > 0);
    assert!(health.last_error.is_some());
    // B's card is untouched by A's degradation.
    assert_eq!(
        poll_card(&[b_active.addr, b_standby.addr])
            .snapshot
            .as_ref()
            .unwrap()
            .io_health
            .failed_reads,
        0
    );

    a_active.stop();
    a_standby.stop();
    b_active.stop();
    b_standby.stop();
}

#[test]
fn a_dropped_pair_faults_its_card_while_the_other_pair_keeps_updating() {
    let a_active = PeerRig::start(Role::Active);
    let a_standby = PeerRig::start(Role::Standby);
    let b_active = PeerRig::start(Role::Active);
    let b_standby = PeerRig::start(Role::Standby);
    let pair_a = [a_active.addr, a_standby.addr];
    let pair_b = [b_active.addr, b_standby.addr];

    // A partial drop first: pair A's standby goes down — the
    // unreachable-peer redundancy fault on the card — while the card's
    // data keeps flowing from the active.
    a_standby.stop();
    let card = poll_card(&pair_a);
    assert_eq!(card.peers[0].as_ref().unwrap().role, Role::Active);
    assert!(
        card.peers[1].is_err(),
        "the dropped standby's fault is recorded"
    );
    assert_eq!(card.active, Some(0));
    assert!(card.snapshot.is_ok());

    // Pair B drops entirely: every peer's fetch fails — the named card
    // fault, page-side. Pair A's card keeps polling and updating.
    b_active.stop();
    b_standby.stop();
    let card_b = poll_card(&pair_b);
    assert!(
        card_b.peers.iter().all(|peer| peer.is_err()),
        "every peer of the dropped pair records unreachable"
    );
    assert_eq!(card_b.active, None);
    assert!(card_b.snapshot.is_err());

    a_active.client.advance(1).unwrap();
    let card_a = poll_card(&pair_a);
    assert_eq!(card_a.active, Some(0));
    assert_eq!(card_a.snapshot.as_ref().unwrap().tick, Tick(1));

    a_active.stop();
}
