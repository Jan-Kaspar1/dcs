//! Loopback-TCP integration tests for `dcs-sim-bus`: the register-mapped
//! `BusDriver` against a live `BusServer`, the shared register bank two
//! clients observe, failure mapping at the `IoDriver` boundary, and
//! identical behavior for a scripted executor run local and
//! register-mapped.

use dcs_core::{
    Direction, DriverDiagnostics, IoDriver, IoError, LinkState, PointId, Quality, QualityReason,
    Sample, Tick, Value, ValueKind,
};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, PointMap, StepError,
};
use dcs_sim::{ChannelId, ChannelMap, PointBinding, SimDriver};
use dcs_sim_bus::{
    BusDriver, BusError, BusRequest, BusResponse, BusServer, LinkError, PointRegister,
    ProcessElement, RegisterBank, RegisterDecl,
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
    with_bank(RegisterBank::new(decls.iter().copied()).unwrap(), test)
}

/// Serves `bank` — declarations plus any merged dynamics — the same
/// way [`with_server`] does.
fn with_bank<R>(bank: RegisterBank, test: impl FnOnce(&BusServer, SocketAddr) -> R) -> R {
    let server = BusServer::bind(("127.0.0.1", 0), bank).unwrap();
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
        executor.scan();
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
        assert_eq!(bus.step(0.1), Ok(Tick(1)));
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
        let tick = active.step(0.1).unwrap();
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
fn a_live_link_reports_connected_health_with_no_last_error() {
    with_server(&fixture_decls(), |_, addr| {
        let bus = BusDriver::connect(addr, &fixture_points()).unwrap();

        // A driver that has never failed reports a live link and no
        // failure history on the diagnostics surface.
        assert_eq!(
            bus.diagnostics(),
            Some(DriverDiagnostics {
                link: LinkState::Connected,
                last_error: None,
                exchange: None,
            })
        );

        // Successful exchanges and register-level refusals leave the
        // link-health surface clear — they are not link failures.
        bus.read(PointId(1)).unwrap();
        assert_eq!(
            bus.write(PointId(1), Value::Bool(true)),
            Err(IoError::TypeMismatch {
                point: PointId(1),
                expected: ValueKind::Float,
                found: Value::Bool(true),
            })
        );
        assert_eq!(
            bus.diagnostics(),
            Some(DriverDiagnostics {
                link: LinkState::Connected,
                last_error: None,
                exchange: None,
            })
        );
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
        assert_eq!(bus.step(0.1), Err(LinkError::Disconnected));
        assert!(!bus.connected());
        assert_eq!(bus.last_failure(), Some(LinkError::Disconnected));
        assert_eq!(bus.read(PointId(1)), Err(IoError::Disconnected(PointId(1))));

        // The diagnostics hook names the same event at link level:
        // disconnected, carrying the failure that severed the link —
        // permanently, since the driver never reconnects.
        assert_eq!(
            bus.diagnostics(),
            Some(DriverDiagnostics {
                link: LinkState::Disconnected,
                last_error: Some("no live connection to the device server".to_string()),
                exchange: None,
            })
        );
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
    // The link-level report carries the failure that severed the link
    // — the timeout, not the Disconnected its consequence produces.
    assert_eq!(
        bus.diagnostics(),
        Some(DriverDiagnostics {
            link: LinkState::Disconnected,
            last_error: Some("device server did not answer in time".to_string()),
            exchange: None,
        })
    );
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
        BusRequest::Step { dt: 0.1 },
        BusRequest::ClaimWriter { owner: 42 },
        BusRequest::ReleaseWriter,
        BusRequest::InjectQuality {
            register: 4,
            quality: Quality::Bad(QualityReason::DeviceFault),
        },
        BusRequest::ClearQuality { register: 4 },
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
        BusResponse::Done,
        BusResponse::Error {
            error: BusError::KindMismatch {
                register: 5,
                expected: ValueKind::Float,
                found: Value::Bool(true),
            },
        },
        BusResponse::Error {
            error: BusError::Fenced {
                detail: "another attachment owns register writes".to_string(),
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
fn the_writer_claim_fences_every_attachment_not_holding_it() {
    with_server(&fixture_decls(), |server, addr| {
        // Two attachments per side — the multi-connection shape one
        // controller presents — plus a reader that never claims.
        let old_a = BusDriver::connect(addr, &fixture_points()).unwrap();
        let old_b = BusDriver::connect(addr, &fixture_points()).unwrap();
        let new_a = BusDriver::connect(addr, &fixture_points()).unwrap();
        let new_b = BusDriver::connect(addr, &fixture_points()).unwrap();
        let observer = BusDriver::connect(addr, &fixture_points()).unwrap();

        // Unclaimed, every attachment writes — the pre-claim behavior.
        old_a.write(PointId(2), Value::Float(1.0)).unwrap();
        old_b.step(0.1).unwrap();
        assert_eq!(observer.read(PointId(2)).unwrap().value, Value::Float(1.0));

        // The old owner's several attachments claim the same token; all
        // of them keep writing.
        old_a.claim_writer(1).unwrap();
        old_b.claim_writer(1).unwrap();
        old_a.write(PointId(2), Value::Float(2.0)).unwrap();
        old_b.step(0.1).unwrap();

        // The takeover claim preempts unconditionally — after it, the
        // old owner's writes and steps are refused at the field, not
        // merely quiesced at its own gate, and a refused write leaves
        // the register untouched rather than double-writing.
        new_a.claim_writer(2).unwrap();
        assert_eq!(
            old_a.write(PointId(2), Value::Float(9.0)),
            Err(IoError::Fenced(PointId(2)))
        );
        assert_eq!(old_b.step(0.1), Err(LinkError::Fenced));
        assert_eq!(
            server.bank().read(9).unwrap(),
            Sample::good(Value::Float(2.0), Tick(1))
        );

        // The claim covers the whole owner token: the takeover side's
        // second attachment claims the same token and writes.
        new_b.claim_writer(2).unwrap();
        new_b.write(PointId(2), Value::Float(3.0)).unwrap();
        new_a.step(0.1).unwrap();
        assert_eq!(observer.read(PointId(2)).unwrap().value, Value::Float(3.0));

        // Reads and the register census stay open to a fenced
        // attachment.
        assert_eq!(old_a.read(PointId(2)).unwrap().value, Value::Float(3.0));
        assert_eq!(old_a.list_registers().unwrap().len(), 3);

        // A fenced attachment reclaims the field only by claiming again
        // — the documented switchback, not an automatic reopen.
        old_a.claim_writer(3).unwrap();
        old_a.write(PointId(2), Value::Float(4.0)).unwrap();
        assert_eq!(
            new_a.write(PointId(2), Value::Float(5.0)),
            Err(IoError::Fenced(PointId(2)))
        );
    });
}

#[test]
fn release_writer_frees_the_field() {
    with_server(&fixture_decls(), |_, addr| {
        let holder = BusDriver::connect(addr, &fixture_points()).unwrap();
        let other = BusDriver::connect(addr, &fixture_points()).unwrap();
        holder.claim_writer(1).unwrap();
        assert_eq!(
            other.write(PointId(2), Value::Float(2.0)),
            Err(IoError::Fenced(PointId(2)))
        );

        // Releasing a claim the attachment does not hold is a no-op —
        // the field stays fenced.
        other.release_writer().unwrap();
        assert_eq!(
            other.write(PointId(2), Value::Float(2.0)),
            Err(IoError::Fenced(PointId(2)))
        );

        // The holder's explicit release frees the field: the other
        // attachment writes without claiming, and its own claim then
        // holds, fencing the released holder out.
        holder.release_writer().unwrap();
        other.write(PointId(2), Value::Float(2.0)).unwrap();
        other.claim_writer(2).unwrap();
        assert_eq!(
            holder.write(PointId(2), Value::Float(9.0)),
            Err(IoError::Fenced(PointId(2)))
        );
    });
}

#[test]
fn injected_quality_stamps_reads_until_cleared_or_overwritten() {
    with_server(&fixture_decls(), |server, addr| {
        let bus = BusDriver::connect(addr, &fixture_points()).unwrap();
        let driver: &dyn IoDriver = &bus;

        // Point 1 maps register 4. The inject stamps the stored
        // sample's declared quality, leaving value and tick untouched;
        // reads through the IoDriver surface and the register census
        // both report it.
        bus.inject_quality(4, Quality::Bad(QualityReason::DeviceFault))
            .unwrap();
        assert_eq!(
            driver.read(PointId(1)).unwrap(),
            Sample::new(
                Value::Float(0.0),
                Quality::Bad(QualityReason::DeviceFault),
                Tick::ZERO,
            )
        );
        assert_eq!(
            server.bank().read(4).unwrap().quality,
            Quality::Bad(QualityReason::DeviceFault)
        );
        assert!(
            bus.list_registers()
                .unwrap()
                .iter()
                .any(|info| info.register == 4
                    && info.sample.quality == Quality::Bad(QualityReason::DeviceFault))
        );

        // A real write overwrites the injection with a Good sample.
        driver.write(PointId(1), Value::Float(1.25)).unwrap();
        assert_eq!(
            driver.read(PointId(1)).unwrap(),
            Sample::good(Value::Float(1.25), Tick::ZERO)
        );

        // Inject again, then clear: the read reports Good on the same
        // stored value and tick.
        bus.inject_quality(4, Quality::Uncertain(QualityReason::Stale))
            .unwrap();
        assert_eq!(
            driver.read(PointId(1)).unwrap().quality,
            Quality::Uncertain(QualityReason::Stale)
        );
        bus.clear_quality(4).unwrap();
        assert_eq!(
            driver.read(PointId(1)).unwrap(),
            Sample::good(Value::Float(1.25), Tick::ZERO)
        );

        // Injecting an unserved register answers the named refusal; the
        // link stays live.
        assert!(matches!(
            bus.inject_quality(12, Quality::Good),
            Err(LinkError::InvalidRequest(_))
        ));
        assert_eq!(
            bus.clear_quality(12),
            Err(LinkError::InvalidRequest(
                "UnknownRegister { register: 12 }".to_string()
            ))
        );
        assert!(bus.connected());
    });
}

#[test]
fn injection_is_open_while_another_attachment_holds_the_writer_claim() {
    with_server(&fixture_decls(), |_, addr| {
        let holder = BusDriver::connect(addr, &fixture_points()).unwrap();
        let tool = BusDriver::connect(addr, &fixture_points()).unwrap();

        // The holder owns the field: the tool's field-mutating requests
        // are fenced, but injection — development tooling — is not.
        holder.claim_writer(1).unwrap();
        assert_eq!(
            tool.write(PointId(2), Value::Float(1.0)),
            Err(IoError::Fenced(PointId(2)))
        );
        assert_eq!(tool.step(0.1), Err(LinkError::Fenced));

        tool.inject_quality(4, Quality::Bad(QualityReason::DeviceFault))
            .unwrap();
        // Both attachments read the declared quality — the fault lands
        // on the shared register the claim holder is serving.
        assert_eq!(
            holder.read(PointId(1)).unwrap().quality,
            Quality::Bad(QualityReason::DeviceFault)
        );
        assert_eq!(
            tool.read(PointId(1)).unwrap().quality,
            Quality::Bad(QualityReason::DeviceFault)
        );

        // Arbitration is undisturbed: the holder still writes and
        // steps, the tool is still fenced out of both.
        holder.write(PointId(2), Value::Float(2.0)).unwrap();
        assert_eq!(holder.step(0.1), Ok(Tick(1)));
        assert_eq!(
            tool.write(PointId(2), Value::Float(9.0)),
            Err(IoError::Fenced(PointId(2)))
        );
        assert_eq!(tool.step(0.1), Err(LinkError::Fenced));

        tool.clear_quality(4).unwrap();
        assert_eq!(
            holder.read(PointId(1)).unwrap(),
            Sample::good(Value::Float(0.0), Tick::ZERO)
        );
    });
}

#[test]
fn identical_scripted_runs_with_injection_produce_identical_samples() {
    let run = || {
        with_server(&fixture_decls(), |_, addr| {
            let bus = BusDriver::connect(addr, &fixture_points()).unwrap();
            let mut trace = Vec::new();
            trace.push(bus.read(PointId(1)).unwrap());
            bus.inject_quality(4, Quality::Bad(QualityReason::CommunicationFault))
                .unwrap();
            trace.push(bus.read(PointId(1)).unwrap());
            bus.step(0.1).unwrap();
            bus.write(PointId(2), Value::Float(1.5)).unwrap();
            // The injection stands across a step and an unrelated write.
            trace.push(bus.read(PointId(1)).unwrap());
            trace.push(bus.read(PointId(2)).unwrap());
            bus.clear_quality(4).unwrap();
            trace.push(bus.read(PointId(1)).unwrap());
            trace
        })
    };
    assert_eq!(run(), run());
}

#[test]
fn disconnect_releases_the_claim_for_a_promoted_peer() {
    with_server(&fixture_decls(), |_, addr| {
        let active = BusDriver::connect(addr, &fixture_points()).unwrap();
        let standby = BusDriver::connect(addr, &fixture_points()).unwrap();
        active.claim_writer(1).unwrap();
        active.write(PointId(2), Value::Float(1.0)).unwrap();
        assert_eq!(
            standby.write(PointId(2), Value::Float(2.0)),
            Err(IoError::Fenced(PointId(2)))
        );

        // The claim is bound to the attachment: the holder's
        // disconnect releases it — a dead owner cannot keep the field
        // fenced. The server observes the close asynchronously, so the
        // probe write retries until the freed field accepts it.
        drop(active);
        let released = (0..100).any(|_| {
            if standby.write(PointId(1), Value::Float(0.0)).is_ok() {
                true
            } else {
                thread::sleep(Duration::from_millis(10));
                false
            }
        });
        assert!(
            released,
            "the claim must release on the holder's disconnect"
        );

        // The promoted peer's claim lands on the freed field, and its
        // writes apply.
        standby.claim_writer(2).unwrap();
        standby.write(PointId(2), Value::Float(2.0)).unwrap();
        assert_eq!(standby.read(PointId(2)).unwrap().value, Value::Float(2.0));
    });
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
            bus.step(0.1).unwrap();
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
                bus.step(0.1).unwrap();
            })
        })
    };
    assert_eq!(run(), run());
}

#[test]
fn a_step_with_an_invalid_dt_is_a_named_refusal_not_a_link_failure() {
    with_server(&fixture_decls(), |_, addr| {
        let bus = BusDriver::connect(addr, &fixture_points()).unwrap();
        // The server refuses a negative or non-finite dt rather than
        // letting the bank's contract panic.
        assert!(matches!(bus.step(-0.5), Err(LinkError::InvalidRequest(_))));
        assert!(matches!(
            bus.step(f64::NAN),
            Err(LinkError::InvalidRequest(_))
        ));
        // The refusal is a protocol answer, not a transport failure:
        // the link stays live and a well-formed step still lands.
        assert!(bus.connected());
        assert_eq!(bus.last_failure(), None);
        assert_eq!(bus.step(0.1), Ok(Tick(1)));
    });
}

// ------------------------------------------------------------------
// Declared dynamics over registers: a `ProcessElement` document bound
// to register addresses, merged into the bank and advanced by the
// protocol's explicit step.
// ------------------------------------------------------------------

/// The station-loop dynamics document — the shared fixture the
/// `dcs-sim-bus-device --dynamics` path merges: Bool-gated pump draws
/// from command registers 20 and 21 into flow registers 12 and 13, a
/// flow sum of registers 11–13 plus a 4.0 inflow bias into net register
/// 14, and an integrator from 14 into level register 10.
const STATION_DYNAMICS: &str = include_str!("../../dcs-sim/fixtures/pump_station_dynamics.json");

/// The register bank the station document drives: level, declared
/// inflow, per-pump draws, net flow, and the two Bool run commands.
fn station_decls() -> Vec<RegisterDecl> {
    [
        (10, Value::Float(0.0)),
        (11, Value::Float(0.0)),
        (12, Value::Float(0.0)),
        (13, Value::Float(0.0)),
        (14, Value::Float(0.0)),
        (20, Value::Bool(false)),
        (21, Value::Bool(false)),
    ]
    .into_iter()
    .map(|(register, initial)| RegisterDecl { register, initial })
    .collect()
}

/// The document parsed into the element list `with_dynamics` merges.
fn station_elements() -> Vec<ProcessElement> {
    serde_json::from_str(STATION_DYNAMICS).unwrap()
}

/// The client-side point map: point 1 reads the level register, point 2
/// writes pump A's run command, point 3 reads pump A's draw.
fn station_points() -> Vec<PointRegister> {
    vec![
        PointRegister {
            point: PointId(1),
            register: 10,
            kind: ValueKind::Float,
        },
        PointRegister {
            point: PointId(2),
            register: 20,
            kind: ValueKind::Bool,
        },
        PointRegister {
            point: PointId(3),
            register: 12,
            kind: ValueKind::Float,
        },
    ]
}

/// Serves the station bank with the fixture dynamics merged.
fn with_station<R>(test: impl FnOnce(&BusServer, SocketAddr) -> R) -> R {
    with_bank(
        RegisterBank::with_dynamics(station_decls(), station_elements()).unwrap(),
        test,
    )
}

#[test]
fn a_command_register_write_moves_the_level_only_while_the_command_stands() {
    with_station(|server, addr| {
        let bus = BusDriver::connect(addr, &station_points()).unwrap();
        let driver: &dyn IoDriver = &bus;
        let level = PointId(1);
        let pump = PointId(2);
        let draw = PointId(3);

        // Element initials seed their output registers: the level
        // starts at 50.0 and the net flow at the 4.0 inflow bias.
        assert_eq!(
            driver.read(level).unwrap(),
            Sample::good(Value::Float(50.0), Tick::ZERO)
        );

        // Pump released: a step fills the well at the inflow rate —
        // 4.0 × 0.5 — and nothing moves without the explicit step.
        bus.step(0.5).unwrap();
        assert_eq!(driver.read(level).unwrap().value, Value::Float(52.0));

        // The command standing, each step draws the level down:
        // on-rate -10 plus the 4.0 bias is net -6, so -3 per 0.5 step.
        driver.write(pump, Value::Bool(true)).unwrap();
        assert_eq!(driver.read(level).unwrap().value, Value::Float(52.0));
        bus.step(0.5).unwrap();
        assert_eq!(driver.read(level).unwrap().value, Value::Float(49.0));
        assert_eq!(
            driver.read(draw).unwrap(),
            Sample::good(Value::Float(-10.0), Tick(2))
        );
        bus.step(0.5).unwrap();
        assert_eq!(driver.read(level).unwrap().value, Value::Float(46.0));

        // Releasing the command stands the off-rate: the draw register
        // returns to 0.0 and the level climbs at the inflow rate again.
        driver.write(pump, Value::Bool(false)).unwrap();
        bus.step(0.5).unwrap();
        assert_eq!(driver.read(draw).unwrap().value, Value::Float(0.0));
        assert_eq!(driver.read(level).unwrap().value, Value::Float(48.0));

        // The server-side bank — the shared field — reports the same
        // samples the client read.
        assert_eq!(
            server.bank().read(10).unwrap(),
            Sample::good(Value::Float(48.0), Tick(4))
        );
        // The dynamics are field state, not controller state: the
        // driver captures nothing.
        assert!(bus.capture_state().is_none());
    });
}

#[test]
fn injected_register_quality_freezes_the_elements_and_propagates() {
    with_station(|_, addr| {
        let bus = BusDriver::connect(addr, &station_points()).unwrap();
        let driver: &dyn IoDriver = &bus;

        // The pump running, one step draws the level to 47.
        driver.write(PointId(2), Value::Bool(true)).unwrap();
        bus.step(0.5).unwrap();
        assert_eq!(driver.read(PointId(1)).unwrap().value, Value::Float(47.0));

        // Fault the command register: the bool_flow's gate reads
        // non-Good, so it freezes its standing -10 draw and stamps the
        // fault on its output; the sum propagates the worst of its
        // inputs, and the integrator freezes the level with it.
        bus.inject_quality(20, Quality::Bad(QualityReason::DeviceFault))
            .unwrap();
        bus.step(0.5).unwrap();
        assert_eq!(
            driver.read(PointId(3)).unwrap(),
            Sample::new(
                Value::Float(-10.0),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(2),
            )
        );
        assert_eq!(
            server_bank(&bus, 10),
            Sample::new(
                Value::Float(47.0),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(2),
            )
        );

        // Clearing the fault resumes the loop: the draw stands again
        // and the level falls.
        bus.clear_quality(20).unwrap();
        bus.step(0.5).unwrap();
        assert_eq!(
            driver.read(PointId(1)).unwrap(),
            Sample::good(Value::Float(44.0), Tick(3))
        );
    });
}

/// Reads `register` through the register census — the
/// non-point-mapped view of the shared bank.
fn server_bank(bus: &BusDriver, register: u16) -> Sample {
    bus.list_registers()
        .unwrap()
        .into_iter()
        .find(|info| info.register == register)
        .unwrap()
        .sample
}

#[test]
fn identical_write_step_scripts_produce_identical_register_traces() {
    let run = || {
        with_station(|_, addr| {
            let bus = BusDriver::connect(addr, &station_points()).unwrap();
            let mut trace = Vec::new();
            for scan in 0..6 {
                // The script: pump A runs on even scans, rests on odd.
                bus.write(PointId(2), Value::Bool(scan % 2 == 0)).unwrap();
                bus.step(0.5).unwrap();
                trace.push(bus.read(PointId(1)).unwrap());
                trace.push(bus.read(PointId(3)).unwrap());
            }
            trace
        })
    };
    assert_eq!(run(), run());
}

#[test]
fn the_dynamics_document_serde_roundtrips() {
    let elements = station_elements();
    let json = serde_json::to_string_pretty(&elements).unwrap();
    assert_eq!(
        serde_json::from_str::<Vec<ProcessElement>>(&json).unwrap(),
        elements
    );
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

/// Writes `document` to a scratch file named for `kind` and returns its
/// path — one per test per kind, so a model and a dynamics document in
/// the same test never collide.
fn scratch_file(kind: &str, document: &str) -> std::path::PathBuf {
    let path = std::env::temp_dir().join(format!(
        "dcs-sim-bus-test-{}-{}-{kind}.json",
        std::process::id(),
        std::thread::current().name().unwrap_or("test")
    ));
    std::fs::write(&path, document).unwrap();
    path
}

/// Writes `document` to a scratch model file and returns its path.
fn model_file(document: &str) -> std::path::PathBuf {
    scratch_file("model", document)
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
        "registers": { "level-raw": { "register": 4, "initial": { "float": 1.5 } } }
      },
      "channels": {
        "level-raw": { "direction": "in", "value_type": "float" }
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
    let (_child, addr) = spawn_device(&path, 2, None);

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

/// A model declaring one `sim-bus` device carrying the station-loop
/// register map the shared dynamics document drives: level register 10,
/// inflow 11, pump draws 12 and 13, net flow 14, run commands 20 and
/// 21 — plus the Bool `sis-active` contact register 30 the protection
/// document's `threshold` drives.
const STATION_MODEL: &str = r#"{
  "version": 1,
  "devices": [
    {
      "id": 3,
      "kind": "sim-bus",
      "parameters": {
        "address": "127.0.0.1:0",
        "registers": {
          "level": 10,
          "inflow": 11,
          "pump-a-flow": 12,
          "pump-b-flow": 13,
          "net-flow": 14,
          "pump-a-run": 20,
          "pump-b-run": 21,
          "sis-active": 30
        }
      },
      "channels": {
        "level": { "direction": "in", "value_type": "float" },
        "inflow": { "direction": "in", "value_type": "float" },
        "pump-a-flow": { "direction": "in", "value_type": "float" },
        "pump-b-flow": { "direction": "in", "value_type": "float" },
        "net-flow": { "direction": "in", "value_type": "float" },
        "pump-a-run": { "direction": "out", "value_type": "bool" },
        "pump-b-run": { "direction": "out", "value_type": "bool" },
        "sis-active": { "direction": "in", "value_type": "bool" }
      }
    }
  ],
  "io_points": [],
  "signals": [],
  "components": [],
  "connections": []
}"#;

/// Spawns the device binary on `model` serving device `id` with any
/// `dynamics` document, and reads the bound address off its serving
/// line.
fn spawn_device(
    model: &std::path::Path,
    id: u64,
    dynamics: Option<&std::path::Path>,
) -> (KillOnDrop, SocketAddr) {
    let mut command = Command::new(DEVICE_BIN);
    command.arg(model).arg("--device").arg(id.to_string());
    if let Some(dynamics) = dynamics {
        command.arg("--dynamics").arg(dynamics);
    }
    let mut child = KillOnDrop(
        command
            .stderr(Stdio::piped())
            .spawn()
            .expect("dcs-sim-bus-device runs"),
    );
    let mut stderr = BufReader::new(child.0.stderr.take().unwrap());
    let mut line = String::new();
    stderr.read_line(&mut line).unwrap();
    let addr: SocketAddr = line
        .split_whitespace()
        .nth(4)
        .and_then(|token| token.parse().ok())
        .unwrap_or_else(|| panic!("unexpected serving line: {line:?}"));
    (child, addr)
}

#[test]
fn the_device_binary_serves_a_dynamics_document_over_registers() {
    let model = model_file(STATION_MODEL);
    let dynamics = scratch_file("dynamics", STATION_DYNAMICS);
    let (_child, addr) = spawn_device(&model, 3, Some(&dynamics));

    // The merged document seeded the level register at the
    // integrator's initial, and a controller's command write moves it
    // on the next explicit step — the station loop over the bus.
    let bus = BusDriver::connect(addr, &station_points()).unwrap();
    let driver: &dyn IoDriver = &bus;
    assert_eq!(driver.read(PointId(1)).unwrap().value, Value::Float(50.0));
    driver.write(PointId(2), Value::Bool(true)).unwrap();
    bus.step(0.5).unwrap();
    assert_eq!(driver.read(PointId(1)).unwrap().value, Value::Float(47.0));
}

#[test]
fn the_device_binary_serves_a_scaled_flow_document_over_registers() {
    // A Float demand register scaled into a draw an integrator
    // consumes: the `scaled_flow` variant merges through the same
    // `--dynamics` seam `dcs-plant-server` serves.
    let model = model_file(STATION_MODEL);
    let dynamics = scratch_file(
        "dynamics",
        r#"[
            {"scaled_flow": {"input": 11, "output": 12, "gain": -0.5, "initial": 0.0}},
            {"integrator": {"input": 12, "output": 10, "initial": 50.0}}
        ]"#,
    );
    let (_child, addr) = spawn_device(&model, 3, Some(&dynamics));

    // Point 1 reads the level register, point 2 writes the demand
    // register, point 3 reads the draw register.
    let bus = BusDriver::connect(
        addr,
        &[
            PointRegister {
                point: PointId(1),
                register: 10,
                kind: ValueKind::Float,
            },
            PointRegister {
                point: PointId(2),
                register: 11,
                kind: ValueKind::Float,
            },
            PointRegister {
                point: PointId(3),
                register: 12,
                kind: ValueKind::Float,
            },
        ],
    )
    .unwrap();
    let driver: &dyn IoDriver = &bus;

    // The integrator seeds the level register at its declared initial.
    assert_eq!(driver.read(PointId(1)).unwrap().value, Value::Float(50.0));

    // The demand standing, the negative gain draws the level down
    // proportionally: -0.5 × 50 = -25 per time unit, -12.5 per 0.5
    // step.
    driver.write(PointId(2), Value::Float(50.0)).unwrap();
    bus.step(0.5).unwrap();
    assert_eq!(driver.read(PointId(3)).unwrap().value, Value::Float(-25.0));
    assert_eq!(driver.read(PointId(1)).unwrap().value, Value::Float(37.5));
}

/// The protection-loop document — the same file
/// `dcs-plant-server --dynamics` merges: a `threshold` on level
/// register 10 driving the `sis-active` Bool register 30, gating the
/// `bool_flow` emergency draw on 12.
const PROTECTION_DYNAMICS: &str = include_str!("../../dcs-sim/fixtures/protection_dynamics.json");

#[test]
fn the_device_binary_serves_a_threshold_protection_document_over_registers() {
    // The Float-to-Bool element merges through the register seam: the
    // level crossing asserts the contact register, which gates the
    // emergency draw — no scheduled script involved.
    let model = model_file(STATION_MODEL);
    let dynamics = scratch_file("dynamics", PROTECTION_DYNAMICS);
    let (_child, addr) = spawn_device(&model, 3, Some(&dynamics));

    // Point 1 reads the level register, point 2 the contact, point 3
    // the draw.
    let bus = BusDriver::connect(
        addr,
        &[
            PointRegister {
                point: PointId(1),
                register: 10,
                kind: ValueKind::Float,
            },
            PointRegister {
                point: PointId(2),
                register: 30,
                kind: ValueKind::Bool,
            },
            PointRegister {
                point: PointId(3),
                register: 12,
                kind: ValueKind::Float,
            },
        ],
    )
    .unwrap();
    let driver: &dyn IoDriver = &bus;
    let contact = |driver: &dyn IoDriver| driver.read(PointId(2)).unwrap().value;

    // The integrator seeds the level at 6.0, the contact released.
    assert_eq!(driver.read(PointId(1)).unwrap().value, Value::Float(6.0));
    assert_eq!(contact(driver), Value::Bool(false));

    // The level climbing past `on` — 6 + 4·1 = 10 — asserts the
    // contact at the tick boundary reading the crossing; the gated
    // draw engages on that same step and pulls the level back.
    bus.step(1.0).unwrap();
    assert_eq!(contact(driver), Value::Bool(false));
    bus.step(1.0).unwrap();
    assert_eq!(contact(driver), Value::Bool(true));
    assert_eq!(driver.read(PointId(3)).unwrap().value, Value::Float(-20.0));
    assert_eq!(driver.read(PointId(1)).unwrap().value, Value::Float(-6.0));

    // Back below `off`, the contact releases cleanly.
    bus.step(1.0).unwrap();
    assert_eq!(contact(driver), Value::Bool(false));
    assert_eq!(driver.read(PointId(3)).unwrap().value, Value::Float(0.0));
}

/// The checked-in dosing-skid dynamics document — the same file
/// `dcs-plant-server --dynamics` merges, bound to register addresses:
/// the summed metered rates feed `injection-rate` (16) and a
/// `dead_time` carries the transport delay to the downstream
/// `discharge-rate` measurement (13).
const DOSING_DYNAMICS: &str = include_str!("../../dcs-demo/fixtures/dosing_skid_dynamics.json");

/// A model declaring one `sim-bus` device carrying every register the
/// checked-in dosing document's elements touch: flow 10/11, tank
/// level 12, discharge 13, net draw 14, refill 15, injection 16, the
/// per-pump draws 30/31 and metered rates 34/35, the Bool run
/// commands 100/101, and the speed demands 110/111.
const DOSING_MODEL: &str = r#"{
  "version": 1,
  "devices": [
    {
      "id": 4,
      "kind": "sim-bus",
      "parameters": {
        "address": "127.0.0.1:0",
        "registers": {
          "flow": 10, "flow-source": 11, "tank-level": 12,
          "discharge-rate": 13, "net-draw": 14, "tank-refill": 15,
          "injection-rate": 16,
          "p1-draw": 30, "p2-draw": 31, "p1-rate": 34, "p2-rate": 35,
          "p1-run": 100, "p2-run": 101,
          "p1-speed": 110, "p2-speed": 111
        }
      },
      "channels": {
        "flow": { "direction": "in", "value_type": "float" },
        "flow-source": { "direction": "in", "value_type": "float" },
        "tank-level": { "direction": "in", "value_type": "float" },
        "discharge-rate": { "direction": "in", "value_type": "float" },
        "net-draw": { "direction": "in", "value_type": "float" },
        "tank-refill": { "direction": "in", "value_type": "float" },
        "injection-rate": { "direction": "in", "value_type": "float" },
        "p1-draw": { "direction": "in", "value_type": "float" },
        "p2-draw": { "direction": "in", "value_type": "float" },
        "p1-rate": { "direction": "in", "value_type": "float" },
        "p2-rate": { "direction": "in", "value_type": "float" },
        "p1-run": { "direction": "out", "value_type": "bool" },
        "p2-run": { "direction": "out", "value_type": "bool" },
        "p1-speed": { "direction": "out", "value_type": "float" },
        "p2-speed": { "direction": "out", "value_type": "float" }
      }
    }
  ],
  "io_points": [],
  "signals": [],
  "components": [],
  "connections": []
}"#;

#[test]
fn the_device_binary_serves_the_dosing_transport_delay_over_registers() {
    // The checked-in document merges through the register seam exactly
    // as it does through `dcs-plant-server --dynamics`: the dead-time
    // element's delayed response is identical either way.
    let model = model_file(DOSING_MODEL);
    let dynamics = scratch_file("dosing-dynamics", DOSING_DYNAMICS);
    let (_child, addr) = spawn_device(&model, 4, Some(&dynamics));

    // Point 1 writes pump A's speed demand register, point 2 reads the
    // injected rate at the injection point, point 3 reads the
    // downstream discharge measurement.
    let bus = BusDriver::connect(
        addr,
        &[
            PointRegister {
                point: PointId(1),
                register: 110,
                kind: ValueKind::Float,
            },
            PointRegister {
                point: PointId(2),
                register: 16,
                kind: ValueKind::Float,
            },
            PointRegister {
                point: PointId(3),
                register: 13,
                kind: ValueKind::Float,
            },
        ],
    )
    .unwrap();
    let driver: &dyn IoDriver = &bus;

    driver.write(PointId(1), Value::Float(50.0)).unwrap();
    // Each explicit step advances the merged elements: the injected
    // rate turns with the first step while the delayed measurement
    // still reads the seeded line, and the pending sample lands at
    // the declared delay — the same response the plant-server merge
    // produces.
    let mut injection = Vec::new();
    let mut discharge = Vec::new();
    for _ in 0..5 {
        bus.step(1.0).unwrap();
        injection.push(driver.read(PointId(2)).unwrap().value);
        discharge.push(driver.read(PointId(3)).unwrap().value);
    }
    assert_eq!(
        injection,
        vec![Value::Float(50.0); 5],
        "the injected rate must turn with the speed demand: {injection:?}"
    );
    assert_eq!(
        discharge,
        vec![
            Value::Float(0.0),
            Value::Float(0.0),
            Value::Float(0.0),
            Value::Float(50.0),
            Value::Float(50.0),
        ],
        "the transport delay must hold the measurement `delay` steps: {discharge:?}"
    );
}

#[test]
fn the_device_binary_reports_named_dynamics_failures() {
    let model = model_file(STATION_MODEL);

    // A malformed document fails the parse naming the file.
    let dynamics = scratch_file("dynamics", "not json");
    let output = Command::new(DEVICE_BIN)
        .arg(&model)
        .arg("--device")
        .arg("3")
        .arg("--dynamics")
        .arg(&dynamics)
        .output()
        .expect("binary runs");
    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("invalid dynamics document"), "{stderr}");

    // A wrongly-kinded endpoint — a bool_flow gate on the Float net
    // register — fails the merge naming the element and its registers.
    let dynamics = scratch_file(
        "dynamics",
        r#"[{"bool_flow": {"input": 14, "output": 12, "on_rate": -1.0, "off_rate": 0.0, "initial": 0.0}}]"#,
    );
    let output = Command::new(DEVICE_BIN)
        .arg(&model)
        .arg("--device")
        .arg("3")
        .arg("--dynamics")
        .arg(&dynamics)
        .output()
        .expect("binary runs");
    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("dynamics element 0"), "{stderr}");
    assert!(stderr.contains("register 14"), "{stderr}");

    // A scaled_flow end on the Bool command register fails the merge
    // the same way.
    let dynamics = scratch_file(
        "dynamics",
        r#"[{"scaled_flow": {"input": 20, "output": 12, "gain": -0.5, "initial": 0.0}}]"#,
    );
    let output = Command::new(DEVICE_BIN)
        .arg(&model)
        .arg("--device")
        .arg("3")
        .arg("--dynamics")
        .arg(&dynamics)
        .output()
        .expect("binary runs");
    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("dynamics element 0"), "{stderr}");
    assert!(stderr.contains("register 20 is Bool"), "{stderr}");

    // An endpoint register the device does not declare is named too.
    let dynamics = scratch_file(
        "dynamics",
        r#"[{"integrator": {"input": 14, "output": 99, "initial": 0.0}}]"#,
    );
    let output = Command::new(DEVICE_BIN)
        .arg(&model)
        .arg("--device")
        .arg("3")
        .arg("--dynamics")
        .arg(&dynamics)
        .output()
        .expect("binary runs");
    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("driving register 99"), "{stderr}");
}
