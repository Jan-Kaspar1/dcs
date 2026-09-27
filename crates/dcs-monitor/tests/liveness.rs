//! The `liveness-reads-stall-behind-wedged-field-io` regression: a
//! monitor whose field connection wedges mid-scan — the `docker pause`
//! shape, the driver's socket held open but unanswered — must keep its
//! liveness reads bounded. `GET /health` and `GET /role` serve the
//! store's published liveness mirror, refreshed by the control plane at
//! every report or scan-stamp change, so the scan's hold on the
//! executor lock — the whole driver timeout — cannot queue them, and
//! `last_scan_age_ms` keeps growing to report the wedge the
//! shared-lock fetch used to stall inside.

use dcs_core::{Direction, PointId, Value, ValueKind};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient};
use dcs_runtime::{Executor, PointMap};
use dcs_sim::{ChannelId, ChannelMap, PointBinding, SimDriver};
use dcs_sim_net::{PlantServer, RemoteDriver};
use std::io::{self, Read, Write};
use std::net::{Shutdown, SocketAddr, TcpListener, TcpStream};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

/// The bound a liveness read owes during the wedge — the QA run's "small
/// bound": probes on the rig rose from ~2 ms mean to a ~4 s stall behind
/// the wedged scan's lock hold; a second is far past every healthy
/// latency and far short of the wedge the test holds.
const ANSWER_BOUND: Duration = Duration::from_secs(1);

/// The remote driver's per-request timeout — long enough that a wedged
/// request cannot time out inside the assertion window, so the scan
/// holds the executor lock for the whole pause exactly like the rig's
/// field-I/O timeout held it.
const DRIVER_TIMEOUT: Duration = Duration::from_secs(10);

fn binding(point: u64, direction: Direction, initial: Value) -> PointBinding {
    PointBinding {
        point: PointId(point),
        channel: ChannelId {
            device: 1,
            name: format!("ch{point}"),
        },
        direction,
        initial,
    }
}

/// The plant the `PlantServer` shares — the same three-point shape the
/// monitor fixtures use.
fn plant_map() -> ChannelMap {
    ChannelMap::new()
        .with_point(binding(10, Direction::In, Value::Float(0.0)))
        .with_point(binding(20, Direction::Out, Value::Float(0.0)))
        .with_point(binding(30, Direction::Out, Value::Float(0.0)))
}

/// The controller's point map for the same points.
fn point_map() -> PointMap {
    PointMap::new()
        .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
        .with_point(PointId(30), Direction::Out, ValueKind::Float)
}

/// The signal index a controller built from the monitor fixture would
/// serve.
fn signal_index() -> SignalIndex {
    PlantModel::load(include_str!("../fixtures/monitor.json"))
        .unwrap()
        .signal_index()
}

/// A pausable TCP relay between the driver's [`RemoteDriver`] and the
/// [`PlantServer`] — the `docker pause` wedge made of real sockets:
/// while `paused` stands, the pump threads hold every connection open
/// and forward nothing, so the driver's in-flight request sits
/// unanswered for its full timeout exactly like a frozen plant
/// process's; released, the buffered bytes flow and the exchange
/// completes — the pause-then-unpause cycle the reproduction ran.
struct PausableRelay {
    addr: SocketAddr,
    paused: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
}

impl PausableRelay {
    /// Binds a relay on an ephemeral loopback port forwarding to
    /// `upstream`, its accept loop on a spawned thread.
    fn forwarding(upstream: SocketAddr) -> io::Result<Self> {
        let listener = TcpListener::bind("127.0.0.1:0")?;
        listener.set_nonblocking(true)?;
        let relay = Self {
            addr: listener.local_addr()?,
            paused: Arc::new(AtomicBool::new(false)),
            stop: Arc::new(AtomicBool::new(false)),
        };
        let (paused, stop) = (relay.paused.clone(), relay.stop.clone());
        thread::spawn(move || {
            while !stop.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((downstream, _)) => {
                        let Ok(served) = TcpStream::connect(upstream) else {
                            continue;
                        };
                        for (from, to) in [
                            (downstream.try_clone().unwrap(), served.try_clone().unwrap()),
                            (served, downstream),
                        ] {
                            let (paused, stop) = (paused.clone(), stop.clone());
                            thread::spawn(move || Self::pump(from, to, paused, stop));
                        }
                    }
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(_) if stop.load(Ordering::Relaxed) => return,
                    Err(_) => thread::sleep(Duration::from_millis(5)),
                }
            }
        });
        Ok(relay)
    }

    /// The address clients connect to.
    fn addr(&self) -> SocketAddr {
        self.addr
    }

    /// The wedge: pumps stop moving bytes while every socket stays
    /// open — a paused plant process's exact wire shape.
    fn pause(&self) {
        self.paused.store(true, Ordering::Relaxed);
    }

    /// Releases the wedge: buffered bytes flow again and every
    /// in-flight exchange completes.
    fn resume(&self) {
        self.paused.store(false, Ordering::Relaxed);
    }

    /// Copies `from` to `to` until either side closes, polling its
    /// read so a pause or the relay's stop lands within a beat.
    fn pump(
        mut from: TcpStream,
        mut to: TcpStream,
        paused: Arc<AtomicBool>,
        stop: Arc<AtomicBool>,
    ) {
        let _ = from.set_read_timeout(Some(Duration::from_millis(10)));
        let mut buf = [0u8; 8192];
        loop {
            if stop.load(Ordering::Relaxed) {
                return;
            }
            if paused.load(Ordering::Relaxed) {
                thread::sleep(Duration::from_millis(5));
                continue;
            }
            match from.read(&mut buf) {
                Ok(0) => {
                    let _ = to.shutdown(Shutdown::Write);
                    return;
                }
                Ok(n) => {
                    if to.write_all(&buf[..n]).is_err() {
                        return;
                    }
                }
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
                    ) => {}
                Err(_) => return,
            }
        }
    }
}

impl Drop for PausableRelay {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}

/// A `GET /health` timed like the reproduction's probe: the answer
/// plus how long it took, so the bound reads in the assertion.
fn probe_health(client: &MonitorClient) -> (dcs_monitor::HealthReport, Duration) {
    let started = Instant::now();
    let report = client
        .health()
        .expect("GET /health stalled behind the wedged scan's lock hold");
    (report, started.elapsed())
}

#[test]
fn liveness_reads_answer_while_the_field_connection_wedges() {
    let plant = PlantServer::bind("127.0.0.1:0", SimDriver::new(plant_map()).unwrap()).unwrap();
    let plant_addr = plant.local_addr().unwrap();
    thread::spawn(move || plant.serve());
    let relay = PausableRelay::forwarding(plant_addr).unwrap();
    let driver = RemoteDriver::connect_with_timeout(relay.addr(), DRIVER_TIMEOUT).unwrap();
    let executor = Executor::new(&driver, point_map(), Vec::new()).unwrap();
    let monitor = Monitor::bind("127.0.0.1:0", executor, signal_index()).unwrap();
    let client = MonitorClient::with_timeout(monitor.local_addr(), ANSWER_BOUND);

    thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        // One live scan: the baseline liveness answers and the mirror's
        // first last-scan stamp.
        monitor.paced_scan();
        let (baseline, elapsed) = probe_health(&client);
        assert!(baseline.live);
        assert!(elapsed < ANSWER_BOUND);
        client.role().unwrap();

        relay.pause();
        // The wedge: a scan parked inside remote field I/O, holding the
        // executor lock for the driver's whole timeout — the shape the
        // rig's paused plant produced.
        let wedged = scope.spawn(|| monitor.paced_scan());
        // Let the scan reach its unanswered read before probing.
        thread::sleep(Duration::from_millis(300));

        let (first, elapsed) = probe_health(&client);
        assert!(first.live);
        assert!(elapsed < ANSWER_BOUND);
        client
            .role()
            .expect("GET /role stalled behind the wedged scan");
        // The reproduction's own contrast probes: the published-copy
        // reads stayed ~5 ms through the same wedge.
        client.snapshot().unwrap();
        client.journal(0).unwrap();

        thread::sleep(Duration::from_millis(500));
        let (second, elapsed) = probe_health(&client);
        assert!(elapsed < ANSWER_BOUND);
        // The wedge's report: `last_scan_age_ms` keeps growing while
        // the scan sits unanswered — the very signal the shared-lock
        // fetch made the endpoint unable to serve.
        match (first.last_scan_age_ms, second.last_scan_age_ms) {
            (Some(earlier), Some(later)) => assert!(
                later > earlier,
                "the wedged scan's age must grow: {earlier}ms then {later}ms"
            ),
            _ => panic!("a completed scan must stamp last_scan_age_ms"),
        }

        relay.resume();
        wedged.join().unwrap();
        // Recovery: the resumed scan completed, and the next one
        // re-stamps the mirror — the reported age drops back to
        // scan-boundary freshness.
        monitor.paced_scan();
        let (recovered, elapsed) = probe_health(&client);
        assert!(elapsed < ANSWER_BOUND);
        assert!(
            recovered.last_scan_age_ms.unwrap() < second.last_scan_age_ms.unwrap(),
            "the post-recovery scan must re-stamp the liveness mirror"
        );

        monitor.shutdown();
    });
}
