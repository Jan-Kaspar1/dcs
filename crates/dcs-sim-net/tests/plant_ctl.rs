//! Loopback integration tests for the `dcs-plant-ctl` development tool:
//! every subcommand run as a subprocess against a fixture `PlantServer`,
//! a tool-injected fault's visibility through a connected controller's
//! snapshot and journal, and the failure surface — an unreachable server
//! and malformed arguments.

use dcs_core::{
    Direction, IoDriver, IoError, JournalEvent, PointId, Quality, QualityReason, Sample, Tick,
    Value, ValueKind,
};
use dcs_model::SignalIndex;
use dcs_monitor::{Monitor, MonitorClient};
use dcs_runtime::{Executor, PointMap};
use dcs_sim::{ChannelId, ChannelMap, Fault, Loopback, PointBinding, SimDriver};
use dcs_sim_net::{PlantResponse, PlantServer, RemoteDriver};
use std::net::{SocketAddr, TcpListener};
use std::process::{Command, Output};
use std::thread;

/// The tool under test, built by Cargo alongside the test harness.
const CTL: &str = env!("CARGO_BIN_EXE_dcs-plant-ctl");

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

/// The fixture plant: a bool field input, and the `Out`/`In` float pair
/// a loopback joins — enough surface for every subcommand.
fn fixture_map() -> ChannelMap {
    ChannelMap::new()
        .with_point(binding(5, Direction::In, Value::Bool(false)))
        .with_point(binding(10, Direction::In, Value::Float(0.0)))
        .with_point(binding(20, Direction::Out, Value::Float(0.0)))
        .with_loopback(Loopback {
            output: PointId(20),
            input: PointId(10),
        })
}

/// `shutdown` on drop, so a panicking test still lets the scoped serve
/// thread exit instead of hanging the scope's join.
struct ShutdownOnDrop<'s>(&'s PlantServer);

impl Drop for ShutdownOnDrop<'_> {
    fn drop(&mut self) {
        self.0.shutdown();
    }
}

/// Serves the fixture plant on an ephemeral loopback port for the
/// duration of `test`.
fn with_server<R>(map: ChannelMap, test: impl FnOnce(SocketAddr) -> R) -> R {
    let server = PlantServer::bind(("127.0.0.1", 0), SimDriver::new(map).unwrap()).unwrap();
    let addr = server.local_addr().unwrap();
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let _guard = ShutdownOnDrop(&server);
        test(addr)
    })
}

/// Runs the tool against the plant at `addr`.
fn ctl(addr: SocketAddr, args: &[&str]) -> Output {
    Command::new(CTL)
        .arg(addr.to_string())
        .args(args)
        .output()
        .expect("dcs-plant-ctl runs")
}

/// Runs the tool with `args` verbatim — for malformed-command-line cases
/// where the address itself is absent.
fn ctl_args(args: &[&str]) -> Output {
    Command::new(CTL)
        .args(args)
        .output()
        .expect("dcs-plant-ctl runs")
}

/// Runs the tool expecting success and returns the server's answer,
/// decoded back into the wire type.
fn ctl_ok(addr: SocketAddr, args: &[&str]) -> PlantResponse {
    let output = ctl(addr, args);
    assert!(
        output.status.success(),
        "{args:?} failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("stdout is a PlantResponse")
}

fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

fn point_sample(snapshot: &dcs_core::TelemetrySnapshot, point: u64) -> Option<Sample> {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == PointId(point))
        .and_then(|telemetry| telemetry.sample)
}

#[test]
fn list_reports_the_fixture_plants_points() {
    with_server(fixture_map(), |addr| {
        let response = ctl_ok(addr, &["list"]);
        let PlantResponse::Points { points } = response else {
            panic!("list answered {response:?}")
        };
        assert_eq!(
            points.iter().map(|info| info.point).collect::<Vec<_>>(),
            vec![PointId(5), PointId(10), PointId(20)]
        );
        assert_eq!(points[0].direction, Direction::In);
        assert_eq!(points[0].sample.value, Value::Bool(false));
        assert_eq!(points[2].direction, Direction::Out);
        assert!(points.iter().all(|info| info.fault.is_none()));
    });
}

#[test]
fn read_write_and_step_roundtrip() {
    with_server(fixture_map(), |addr| {
        assert_eq!(
            ctl_ok(addr, &["read", "10"]),
            PlantResponse::Sample {
                sample: Sample::good(Value::Float(0.0), Tick::ZERO)
            }
        );

        // Field-input writes: an integer is Int, a float Float, a bool
        // literal Bool — and the point's declared kind still rules.
        assert_eq!(ctl_ok(addr, &["write", "10", "2.5"]), PlantResponse::Done);
        assert_eq!(ctl_ok(addr, &["write", "5", "true"]), PlantResponse::Done);
        let remote = RemoteDriver::connect(addr).unwrap();
        assert_eq!(remote.read(PointId(10)).unwrap().value, Value::Float(2.5));
        assert_eq!(remote.read(PointId(5)).unwrap().value, Value::Bool(true));

        // Stepping advances the shared plant and routes the loopback.
        assert_eq!(
            ctl_ok(addr, &["step", "0.5"]),
            PlantResponse::Stepped { tick: Tick(1) }
        );
        assert_eq!(ctl_ok(addr, &["write", "20", "3.5"]), PlantResponse::Done);
        assert_eq!(
            ctl_ok(addr, &["step", "0.5"]),
            PlantResponse::Stepped { tick: Tick(2) }
        );
        assert_eq!(
            ctl_ok(addr, &["read", "10"]),
            PlantResponse::Sample {
                sample: Sample::good(Value::Float(3.5), Tick(2))
            }
        );
    });
}

#[test]
fn fault_and_clear_fault_roundtrip() {
    with_server(fixture_map(), |addr| {
        let remote = RemoteDriver::connect(addr).unwrap();

        assert_eq!(
            ctl_ok(addr, &["fault", "10", "bad:device_fault"]),
            PlantResponse::Done
        );
        assert_eq!(
            remote.read(PointId(10)).unwrap().quality,
            Quality::Bad(QualityReason::DeviceFault)
        );
        // The injected fault shows in the listing too.
        let PlantResponse::Points { points } = ctl_ok(addr, &["list"]) else {
            panic!("list failed")
        };
        assert_eq!(
            points
                .iter()
                .find(|info| info.point == PointId(10))
                .unwrap()
                .fault,
            Some(Fault::Quality(Quality::Bad(QualityReason::DeviceFault)))
        );

        // An error fault makes accesses fail; clearing restores them.
        assert_eq!(
            ctl_ok(addr, &["fault", "5", "timeout"]),
            PlantResponse::Done
        );
        assert_eq!(remote.read(PointId(5)), Err(IoError::Timeout(PointId(5))));

        assert_eq!(ctl_ok(addr, &["clear-fault", "10"]), PlantResponse::Done);
        assert_eq!(ctl_ok(addr, &["clear-fault", "5"]), PlantResponse::Done);
        assert!(remote.read(PointId(10)).unwrap().quality.is_good());
        assert_eq!(remote.read(PointId(5)).unwrap().value, Value::Bool(false));
    });
}

#[test]
fn an_injected_fault_is_visible_in_a_controllers_snapshot_and_journal() {
    with_server(fixture_map(), |addr| {
        // A connected controller: an executor scanning the shared plant
        // through a RemoteDriver, served by a Monitor like the demo's
        // monitoring page.
        let remote = RemoteDriver::connect(addr).unwrap();
        let map: PointMap = [
            (PointId(10), dcs_core::Direction::In, ValueKind::Float),
            (PointId(20), dcs_core::Direction::Out, ValueKind::Float),
        ]
        .into_iter()
        .collect();
        let executor = Executor::new(&remote, map, vec![]).unwrap();
        let monitor =
            Monitor::bind("127.0.0.1:0", executor, SignalIndex { points: vec![] }).unwrap();
        let client = MonitorClient::new(monitor.local_addr());
        thread::scope(|scope| {
            scope.spawn(|| monitor.serve());

            let snapshot = client.advance(1).unwrap();
            assert_eq!(point_sample(&snapshot, 10).unwrap().quality, Quality::Good);

            // Perturb the live plant through the tool, from outside the
            // controller process.
            assert_eq!(
                ctl_ok(addr, &["fault", "10", "bad:device_fault"]),
                PlantResponse::Done
            );

            // The fault is shared field state: a RemoteDriver read sees
            // it directly, and the controller's next scan reports it in
            // the snapshot and journals the quality transition.
            assert_eq!(
                remote.read(PointId(10)).unwrap().quality,
                Quality::Bad(QualityReason::DeviceFault)
            );
            let snapshot = client.advance(1).unwrap();
            assert_eq!(
                point_sample(&snapshot, 10).unwrap().quality,
                Quality::Bad(QualityReason::DeviceFault)
            );
            let journal = client.journal(0).unwrap();
            assert!(
                journal.iter().any(|entry| entry.event
                    == JournalEvent::QualityChanged {
                        point: PointId(10),
                        from: Some(Quality::Good),
                        to: Quality::Bad(QualityReason::DeviceFault),
                    }),
                "journal lacks the quality transition: {journal:?}"
            );
            monitor.shutdown();
        });
    });
}

#[test]
fn an_unreachable_server_exits_nonzero_naming_the_address() {
    // Bind once to learn a free port, then drop the listener so the
    // address refuses connections.
    let addr = TcpListener::bind(("127.0.0.1", 0))
        .unwrap()
        .local_addr()
        .unwrap();
    let output = ctl(addr, &["list"]);
    assert!(!output.status.success());
    let stderr = stderr(&output);
    assert!(stderr.contains(&addr.to_string()), "{stderr}");
}

#[test]
fn server_reported_errors_exit_nonzero() {
    with_server(fixture_map(), |addr| {
        // A point the plant does not serve.
        let output = ctl(addr, &["read", "99"]);
        assert!(!output.status.success());
        assert!(stderr(&output).contains("unknown I/O point"), "{output:?}");

        // A value kind the point does not declare.
        let output = ctl(addr, &["write", "5", "1.5"]);
        assert!(!output.status.success());
        assert!(stderr(&output).contains("expects"), "{output:?}");

        // A step the protocol refuses — negative dt is a well-formed
        // request the server rejects, not a usage error.
        let output = ctl(addr, &["step", "-0.5"]);
        assert!(!output.status.success());
        assert!(
            stderr(&output).contains("finite and non-negative"),
            "{output:?}"
        );
    });
}

#[test]
fn malformed_arguments_fail_with_usage_never_a_panic() {
    // The address is dead on purpose: argument validation must precede
    // any connection.
    let dead = "127.0.0.1:9";
    let cases: Vec<Vec<&str>> = vec![
        vec![],
        vec![dead],
        vec![dead, "bogus"],
        vec![dead, "list", "extra"],
        vec![dead, "read"],
        vec![dead, "read", "abc"],
        vec![dead, "read", "10", "extra"],
        vec![dead, "write", "10"],
        vec![dead, "write", "10", "abc"],
        vec![dead, "write", "10", "nan"],
        vec![dead, "fault", "10"],
        vec![dead, "fault", "10", "loud"],
        vec![dead, "fault", "10", "bad:nonsense"],
        vec![dead, "fault", "10", "timeout:stale"],
        vec![dead, "clear-fault"],
        vec![dead, "step"],
        vec![dead, "step", "abc"],
        vec![dead, "step", "nan"],
    ];
    for args in &cases {
        let output = ctl_args(args);
        assert!(!output.status.success(), "{args:?} unexpectedly succeeded");
        let stderr = stderr(&output);
        assert!(stderr.contains("usage:"), "{args:?}: {stderr}");
        assert!(!stderr.contains("panic"), "{args:?}: {stderr}");
    }
}
