//! Loopback-TCP integration tests for `dcs-sim-bus`: the register-mapped
//! `BusDriver` against a live `BusServer`, the shared register bank two
//! clients observe, failure mapping at the `IoDriver` boundary, and
//! identical behavior for a scripted executor run local and
//! register-mapped.

use dcs_core::{IoDriver, IoError, PointId, Sample, Tick, Value, ValueKind};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, PointMap, StepError,
};
use dcs_sim::{ChannelId, ChannelMap, Direction, PointBinding, SimDriver};
use dcs_sim_bus::{
    BusDriver, BusError, BusRequest, BusResponse, BusServer, LinkError, PointRegister,
    RegisterBank, RegisterDecl,
};
use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::Duration;

/// The device-server binary under test, built by Cargo alongside the
/// test harness.
const DEVICE_BIN: &str = env!("CARGO_BIN_EXE_dcs-sim-bus-device");

/// The fixture register bank: a float input register, a float output
/// register, and a bool register — enough surface for every test.
fn fixture_decls() -> Vec<RegisterDecl> {
    vec![
        RegisterDecl {
            register: 4,
            initial: Value::Float(0.0),
        },
        RegisterDecl {
            register: 7,
            initial: Value::Bool(false),
        },
        RegisterDecl {
            register: 9,
            initial: Value::Float(0.0),
        },
    ]
}

/// The fixture point map: the `In` point 1 reads register 4, the `Out`
/// point 2 writes register 9, the `In` point 5 reads register 7.
fn fixture_points() -> Vec<PointRegister> {
    vec![
        PointRegister {
            point: PointId(1),
            register: 4,
            kind: ValueKind::Float,
        },
        PointRegister {
            point: PointId(2),
            register: 9,
            kind: ValueKind::Float,
        },
        PointRegister {
            point: PointId(5),
            register: 7,
            kind: ValueKind::Bool,
        },
    ]
}

/// The local-sim equivalent of the fixture's points — the reference the
/// register-mapped driver must behave identically to.
fn local_map() -> ChannelMap {
    let binding = |point: u64, direction: Direction, initial: Value| PointBinding {
        point: PointId(point),
        channel: ChannelId {
            device: 1,
            name: format!("ch{point}"),
        },
        direction,
        initial,
    };
    ChannelMap::new()
        .with_point(binding(1, Direction::In, Value::Float(0.0)))
        .with_point(binding(2, Direction::Out, Value::Float(0.0)))
        .with_point(binding(5, Direction::In, Value::Bool(false)))
}

/// `shutdown` on drop, so a panicking test still lets the scoped serve
/// thread exit instead of hanging the scope's join.
struct ShutdownOnDrop<'s>(&'s BusServer);

impl Drop for ShutdownOnDrop<'_> {
    fn drop(&mut self) {
        self.0.shutdown();
    }
}

/// Serves `decls`'s register bank on an ephemeral loopback port for the
/// duration of `test`, then shuts the server down and joins its accept
/// thread.
fn with_server<R>(decls: &[RegisterDecl], test: impl FnOnce(&BusServer, SocketAddr) -> R) -> R {
    let server = BusServer::bind(
        ("127.0.0.1", 0),
        RegisterBank::new(decls.iter().copied()).unwrap(),
    )
    .unwrap();
    let addr = server.local_addr().unwrap();
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let _guard = ShutdownOnDrop(&server);
        test(&server, addr)
    })
}

/// A stateful test component: integrates its input and writes the
/// running total, so a transport difference would show up in the trace.
struct Accumulator {
    input: PointId,
    output: PointId,
    total: f64,
}

impl Component for Accumulator {
    fn name(&self) -> &str {
        "accumulator"
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("in", self.input),
            IoRequirement::output::<f64>("out", self.output),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        let input = io.read_typed::<f64>(self.input)?;
        if input.quality.is_good() {
            self.total += input.value;
        }
        io.write_typed::<f64>(self.output, self.total)?;
        Ok(())
    }
}

/// Runs the scripted scenario against `driver`: 20 scans of an
/// `Accumulator` wired to points 1 and 2, the field input scripted to a
/// quarter-per-tick ramp written through the driver before each scan,
/// each scan followed by one explicit step. Returns the executor-image
/// sample sequence — the component-visible behavior the driver
/// produced.
fn scripted_run(driver: &(dyn IoDriver + Sync), mut step: impl FnMut()) -> Vec<Sample> {
    let point_map: PointMap = [
        (PointId(1), dcs_core::Direction::In, ValueKind::Float),
        (PointId(2), dcs_core::Direction::Out, ValueKind::Float),
    ]
    .into_iter()
    .collect();
    let mut executor = Executor::new(
        driver,
        point_map,
        vec![Box::new(Accumulator {
            input: PointId(1),
            output: PointId(2),
            total: 0.0,
        })],
    )
    .unwrap();
    let mut trace = Vec::new();
    for scan in 0..20 {
        // The script: the field input ramps a quarter per scan, written
        // through the same IoDriver surface on both transports.
        driver
            .write(PointId(1), Value::Float(scan as f64 * 0.25))
            .unwrap();
        executor.scan().unwrap();
        step();
        trace.push(executor.sample(PointId(1)).unwrap());
        trace.push(executor.sample(PointId(2)).unwrap());
    }
    trace
}

#[test]
fn write_then_read_through_the_register_protocol_roundtrips_a_value() {
    with_server(&fixture_decls(), |server, addr| {
        let bus = BusDriver::connect(addr, &fixture_points()).unwrap();
        let driver: &dyn IoDriver = &bus;

        driver.write(PointId(2), Value::Float(3.5)).unwrap();
        let sample = driver.read(PointId(2)).unwrap();
        assert_eq!(sample.value, Value::Float(3.5));
        assert!(sample.quality.is_good());
        // The write stamped the bank's current tick — still Tick::ZERO
        // until the explicit step.
        assert_eq!(sample.tick, Tick::ZERO);

        // The typed handles over the register driver decode identically.
        assert_eq!(
            dcs_core::Input::<f64>::new(driver, PointId(2))
                .read()
                .unwrap()
                .value,
            3.5
        );
        dcs_core::Output::<f64>::new(driver, PointId(2))
            .write(4.5)
            .unwrap();
        assert_eq!(
            dcs_core::Input::<f64>::new(driver, PointId(2))
                .read()
                .unwrap()
                .value,
            4.5
        );

        // The explicit step advances the device tick; the next write
        // stamps it.
        assert_eq!(bus.step(), Ok(Tick(1)));
        driver.write(PointId(2), Value::Float(5.5)).unwrap();
        assert_eq!(
            driver.read(PointId(2)).unwrap(),
            Sample::good(Value::Float(5.5), Tick(1))
        );

        // The census lists every served register by address; the bank
        // the server shares shows the same state.
        let registers = bus.list_registers().unwrap();
        assert_eq!(
            registers
                .iter()
                .map(|info| info.register)
                .collect::<Vec<_>>(),
            vec![4, 7, 9]
        );
        assert_eq!(
            server.bank().read(9).unwrap(),
            Sample::good(Value::Float(5.5), Tick(1))
        );
    });
}

#[test]
fn a_second_client_observes_the_same_stepped_registers() {
    with_server(&fixture_decls(), |_, addr| {
        let active = BusDriver::connect(addr, &fixture_points()).unwrap();
        let standby = BusDriver::connect(addr, &fixture_points()).unwrap();

        active.write(PointId(2), Value::Float(3.5)).unwrap();

        // Both clients read the same stored sample — the standby's view
        // of the device is the active's.
        let seen_by_active = active.read(PointId(2)).unwrap();
        let seen_by_standby = standby.read(PointId(2)).unwrap();
        assert_eq!(seen_by_active, seen_by_standby);
        assert_eq!(seen_by_active, Sample::good(Value::Float(3.5), Tick::ZERO));

        // The explicit step advances the shared bank's tick; a write
        // the active makes then stamps it, and the standby sees that.
        let tick = active.step().unwrap();
        active.write(PointId(2), Value::Float(4.5)).unwrap();
        assert_eq!(
            standby.read(PointId(2)).unwrap(),
            Sample::good(Value::Float(4.5), tick)
        );
    });
}

#[test]
fn protocol_answers_map_to_named_io_errors() {
    // The bank holds registers 4, 7, 9; the driver additionally maps
    // point 6 onto register 12, which the device does not serve, and
    // point 8 declares Float on register 7, which holds a Bool.
    let points = [
        fixture_points(),
        vec![
            PointRegister {
                point: PointId(6),
                register: 12,
                kind: ValueKind::Float,
            },
            PointRegister {
                point: PointId(8),
                register: 7,
                kind: ValueKind::Float,
            },
        ],
    ]
    .concat();
    with_server(&fixture_decls(), |_, addr| {
        let bus = BusDriver::connect(addr, &points).unwrap();
        let driver: &dyn IoDriver = &bus;

        // A point the driver's map does not serve is refused before any
        // request leaves.
        assert_eq!(
            driver.read(PointId(99)),
            Err(IoError::UnknownPoint(PointId(99)))
        );
        assert_eq!(
            driver.write(PointId(99), Value::Float(1.0)),
            Err(IoError::UnknownPoint(PointId(99)))
        );

        // A write carrying the wrong kind for the mapped point is
        // refused likewise.
        assert_eq!(
            driver.write(PointId(1), Value::Bool(true)),
            Err(IoError::TypeMismatch {
                point: PointId(1),
                expected: ValueKind::Float,
                found: Value::Bool(true),
            })
        );

        // The server reports an unmapped register as UnknownPoint at the
        // point the caller addressed.
        assert_eq!(
            driver.read(PointId(6)),
            Err(IoError::UnknownPoint(PointId(6)))
        );

        // A register holding another kind than the point declares is a
        // TypeMismatch on read and on write — never a coercion.
        assert_eq!(
            driver.read(PointId(8)),
            Err(IoError::TypeMismatch {
                point: PointId(8),
                expected: ValueKind::Float,
                found: Value::Bool(false),
            })
        );
        assert_eq!(
            driver.write(PointId(8), Value::Float(1.0)),
            Err(IoError::TypeMismatch {
                point: PointId(8),
                expected: ValueKind::Bool,
                found: Value::Float(1.0),
            })
        );

        // Register-level refusals are not link failures: the connection
        // stays live and the link-health surface stays clear.
        assert!(bus.connected());
        assert_eq!(bus.last_failure(), None);
        assert_eq!(driver.read(PointId(1)).unwrap().value, Value::Float(0.0));
    });
}

#[test]
fn stopping_the_server_surfaces_disconnected_not_panics() {
    let server = BusServer::bind(
        ("127.0.0.1", 0),
        RegisterBank::new(fixture_decls()).unwrap(),
    )
    .unwrap();
    let addr = server.local_addr().unwrap();
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let bus = BusDriver::connect(addr, &fixture_points()).unwrap();
        assert!(bus.read(PointId(1)).is_ok());

        server.shutdown();
        // Every access now fails with Disconnected — at the boundary, as
        // named errors — and the dead driver does not silently recover.
        assert_eq!(bus.read(PointId(1)), Err(IoError::Disconnected(PointId(1))));
        assert_eq!(
            bus.write(PointId(2), Value::Float(1.0)),
            Err(IoError::Disconnected(PointId(2)))
        );
        assert_eq!(bus.step(), Err(LinkError::Disconnected));
        assert!(!bus.connected());
        assert_eq!(bus.last_failure(), Some(LinkError::Disconnected));
        assert_eq!(bus.read(PointId(1)), Err(IoError::Disconnected(PointId(1))));
    });
}

#[test]
fn an_unresponsive_peer_surfaces_timeout_then_disconnects() {
    // A listener that never answers: the connection completes out of
    // the backlog but no response ever arrives.
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let addr = listener.local_addr().unwrap();
    let bus = BusDriver::connect_with_timeout(addr, Duration::from_millis(200), &fixture_points())
        .unwrap();

    assert_eq!(bus.read(PointId(1)), Err(IoError::Timeout(PointId(1))));
    // A late answer could pair with a later request, so the link is
    // dropped: the next access fails fast as Disconnected.
    assert_eq!(bus.read(PointId(1)), Err(IoError::Disconnected(PointId(1))));
    assert_eq!(bus.last_failure(), Some(LinkError::Timeout));
    drop(listener);
}

#[test]
fn connecting_to_a_dead_port_fails_cleanly() {
    // Bind once to learn a free port, then drop the listener so the
    // address refuses connections.
    let addr = TcpListener::bind(("127.0.0.1", 0))
        .unwrap()
        .local_addr()
        .unwrap();
    let error = BusDriver::connect(addr, &fixture_points()).unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::ConnectionRefused);
}

#[test]
fn a_malformed_frame_is_refused_without_killing_the_connection() {
    with_server(&fixture_decls(), |_, addr| {
        let mut stream = TcpStream::connect(addr).unwrap();
        // A frame whose payload decodes to no request: unknown tag 0x7f
        // with trailing bytes. The documented answer is an error frame —
        // tag 0x05, code 0x03 (invalid request), detail text.
        stream.write_all(&[0, 3, 0x7f, 0, 0]).unwrap();
        let mut header = [0u8; 2];
        stream.read_exact(&mut header).unwrap();
        let length = u16::from_be_bytes(header) as usize;
        let mut body = vec![0u8; length];
        stream.read_exact(&mut body).unwrap();
        assert_eq!(body[0], 0x05, "expected an error frame: {body:?}");
        assert_eq!(body[1], 0x03, "expected invalid_request: {body:?}");

        // The connection still serves a well-formed read of register 4:
        // request tag 0x01, sample response tag 0x01, float kind 0x03.
        stream.write_all(&[0, 3, 0x01, 0, 4]).unwrap();
        stream.read_exact(&mut header).unwrap();
        let length = u16::from_be_bytes(header) as usize;
        let mut body = vec![0u8; length];
        stream.read_exact(&mut body).unwrap();
        assert_eq!(body[0], 0x01, "expected a sample frame: {body:?}");
        assert_eq!(body[1], 0x03, "expected a float value: {body:?}");
    });
}

#[test]
fn an_oversized_frame_ends_the_connection() {
    with_server(&fixture_decls(), |_, addr| {
        let mut stream = TcpStream::connect(addr).unwrap();
        // An announced payload beyond the documented bound is a
        // protocol violation: the server drops the connection rather
        // than reading it.
        stream.write_all(&u16::MAX.to_be_bytes()).unwrap();
        let mut byte = [0u8; 1];
        assert!(
            matches!(stream.read(&mut byte), Ok(0) | Err(_)),
            "the server must have dropped the violating connection"
        );
    });
}

#[test]
fn protocol_contract_types_serde_roundtrip() {
    let requests = [
        BusRequest::ReadRegister { register: 4 },
        BusRequest::WriteRegister {
            register: 9,
            value: Value::Float(1.5),
        },
        BusRequest::ListRegisters,
        BusRequest::Step,
    ];
    for request in requests {
        let json = serde_json::to_string(&request).unwrap();
        assert_eq!(serde_json::from_str::<BusRequest>(&json).unwrap(), request);
    }
    let responses = [
        BusResponse::Sample {
            sample: Sample::good(Value::Int(-3), Tick(9)),
        },
        BusResponse::Written { tick: Tick(7) },
        BusResponse::Stepped { tick: Tick(8) },
        BusResponse::Error {
            error: BusError::KindMismatch {
                register: 5,
                expected: ValueKind::Float,
                found: Value::Bool(true),
            },
        },
    ];
    for response in responses {
        let json = serde_json::to_string(&response).unwrap();
        assert_eq!(
            serde_json::from_str::<BusResponse>(&json).unwrap(),
            response
        );
    }
}

#[test]
fn executor_runs_unchanged_with_identical_behavior_local_and_register_mapped() {
    let local = SimDriver::new(local_map()).unwrap();
    let local_trace = scripted_run(&local, || {
        local.step(0.1);
    });
    let bus_trace = with_server(&fixture_decls(), |_, addr| {
        let bus = BusDriver::connect(addr, &fixture_points()).unwrap();
        scripted_run(&bus, || {
            bus.step().unwrap();
        })
    });
    // Same components, same script, same point map — only the transport
    // differs, and the observed samples are identical.
    assert_eq!(local_trace, bus_trace);
}

#[test]
fn two_identical_scripted_runs_produce_identical_sample_sequences() {
    let run = || {
        with_server(&fixture_decls(), |_, addr| {
            let bus = BusDriver::connect(addr, &fixture_points()).unwrap();
            scripted_run(&bus, || {
                bus.step().unwrap();
            })
        })
    };
    assert_eq!(run(), run());
}

/// `kill` and `wait` on drop, so a panicking test never leaves the
/// device-server process running.
struct KillOnDrop(Child);

impl Drop for KillOnDrop {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

/// Writes `document` to a scratch model file and returns its path.
fn model_file(document: &str) -> std::path::PathBuf {
    let path = std::env::temp_dir().join(format!(
        "dcs-sim-bus-test-{}-{}.json",
        std::process::id(),
        std::thread::current().name().unwrap_or("test")
    ));
    std::fs::write(&path, document).unwrap();
    path
}

/// A minimal model declaring one `sim-bus` device: `level-raw` at
/// register 4 powered on at 1.5, served on an ephemeral port.
const BIN_MODEL: &str = r#"{
  "version": 1,
  "devices": [
    {
      "id": 2,
      "kind": "sim-bus",
      "parameters": {
        "address": "127.0.0.1:0",
        "registers": { "level-raw": { "register": 4, "initial": { "Float": 1.5 } } }
      },
      "channels": {
        "level-raw": { "direction": "in", "value_type": "Float" }
      }
    }
  ],
  "io_points": [],
  "signals": [],
  "components": [],
  "connections": []
}"#;

#[test]
fn the_device_binary_serves_the_models_declared_registers() {
    let path = model_file(BIN_MODEL);
    let mut child = KillOnDrop(
        Command::new(DEVICE_BIN)
            .arg(&path)
            .arg("--device")
            .arg("2")
            .stderr(Stdio::piped())
            .spawn()
            .expect("dcs-sim-bus-device runs"),
    );

    // The serving line on stderr carries the bound address.
    let mut stderr = BufReader::new(child.0.stderr.take().unwrap());
    let mut line = String::new();
    stderr.read_line(&mut line).unwrap();
    let addr: SocketAddr = line
        .split_whitespace()
        .nth(4)
        .and_then(|token| token.parse().ok())
        .unwrap_or_else(|| panic!("unexpected serving line: {line:?}"));

    // The register map came from the model: register 4 holds the
    // declared initial, and a register-protocol client can drive it.
    let bus = BusDriver::connect(
        addr,
        &[PointRegister {
            point: PointId(11),
            register: 4,
            kind: ValueKind::Float,
        }],
    )
    .unwrap();
    assert_eq!(
        bus.read(PointId(11)).unwrap(),
        Sample::good(Value::Float(1.5), Tick::ZERO)
    );
    bus.write(PointId(11), Value::Float(9.25)).unwrap();
    assert_eq!(bus.read(PointId(11)).unwrap().value, Value::Float(9.25));
}

#[test]
fn the_device_binary_reports_named_startup_failures() {
    let path = model_file(BIN_MODEL);

    // Argument errors print usage and exit 2.
    let output = Command::new(DEVICE_BIN).output().expect("binary runs");
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("Usage:"), "{stderr}");

    // A device the model does not declare fails startup naming it.
    let output = Command::new(DEVICE_BIN)
        .arg(&path)
        .arg("--device")
        .arg("7")
        .output()
        .expect("binary runs");
    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("device 7"), "{stderr}");
}
