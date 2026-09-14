//! Loopback integration tests for the `dcs-sim-bus-ctl` development
//! tool: every subcommand run as a subprocess against a fixture
//! `BusServer`, write-then-read consistency through the register
//! protocol, the failure surface — an unreachable server, server error
//! answers, malformed arguments — and identical output for identical
//! request sequences.

use dcs_core::{PointId, Sample, Tick, Value, ValueKind};
use dcs_sim_bus::{BusDriver, BusResponse, BusServer, PointRegister, RegisterBank, RegisterDecl};
use std::net::{SocketAddr, TcpListener};
use std::process::{Command, Output};
use std::thread;

/// The tool under test, built by Cargo alongside the test harness.
const CTL: &str = env!("CARGO_BIN_EXE_dcs-sim-bus-ctl");

/// The fixture register bank: one register per value kind — a float, a
/// bool, and an int — enough surface for every subcommand.
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
            initial: Value::Int(0),
        },
    ]
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

/// Runs the tool against the device server at `addr`.
fn ctl(addr: SocketAddr, args: &[&str]) -> Output {
    Command::new(CTL)
        .arg(addr.to_string())
        .args(args)
        .output()
        .expect("dcs-sim-bus-ctl runs")
}

/// Runs the tool with `args` verbatim — for malformed-command-line cases
/// where the address itself is absent.
fn ctl_args(args: &[&str]) -> Output {
    Command::new(CTL)
        .args(args)
        .output()
        .expect("dcs-sim-bus-ctl runs")
}

/// Runs the tool expecting success and returns the server's answer,
/// decoded back into the wire type.
fn ctl_ok(addr: SocketAddr, args: &[&str]) -> BusResponse {
    let output = ctl(addr, args);
    assert!(
        output.status.success(),
        "{args:?} failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("stdout is a BusResponse")
}

fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

#[test]
fn list_reports_every_registers_address_kind_and_stored_sample() {
    with_server(&fixture_decls(), |_, addr| {
        let BusResponse::Registers { registers } = ctl_ok(addr, &["list"]) else {
            panic!("list answered with the wrong variant")
        };
        assert_eq!(
            registers
                .iter()
                .map(|info| info.register)
                .collect::<Vec<_>>(),
            vec![4, 7, 9]
        );
        assert_eq!(
            registers[0].sample,
            Sample::good(Value::Float(0.0), Tick::ZERO)
        );
        assert_eq!(registers[1].sample.value.kind(), ValueKind::Bool);
        assert_eq!(registers[2].sample.value.kind(), ValueKind::Int);
    });
}

#[test]
fn read_write_and_step_roundtrip() {
    with_server(&fixture_decls(), |server, addr| {
        assert_eq!(
            ctl_ok(addr, &["read", "4"]),
            BusResponse::Sample {
                sample: Sample::good(Value::Float(0.0), Tick::ZERO)
            }
        );

        // Writes interpret the literal as the register's declared kind:
        // "1" on the float register stores Float(1.0) — not the Int a
        // generic parse would send for the server to refuse.
        assert_eq!(
            ctl_ok(addr, &["write", "4", "2.5"]),
            BusResponse::Written { tick: Tick::ZERO }
        );
        assert_eq!(
            ctl_ok(addr, &["write", "4", "1"]),
            BusResponse::Written { tick: Tick::ZERO }
        );
        assert_eq!(
            ctl_ok(addr, &["read", "4"]),
            BusResponse::Sample {
                sample: Sample::good(Value::Float(1.0), Tick::ZERO)
            }
        );

        // The bool and int registers take their own literal shapes.
        assert_eq!(
            ctl_ok(addr, &["write", "7", "true"]),
            BusResponse::Written { tick: Tick::ZERO }
        );
        assert_eq!(
            ctl_ok(addr, &["write", "9", "-3"]),
            BusResponse::Written { tick: Tick::ZERO }
        );
        assert_eq!(
            ctl_ok(addr, &["read", "7"]),
            BusResponse::Sample {
                sample: Sample::good(Value::Bool(true), Tick::ZERO)
            }
        );
        assert_eq!(
            ctl_ok(addr, &["read", "9"]),
            BusResponse::Sample {
                sample: Sample::good(Value::Int(-3), Tick::ZERO)
            }
        );

        // `step` advances the bank's tick once; `step n` n times.
        assert_eq!(
            ctl_ok(addr, &["step"]),
            BusResponse::Stepped { tick: Tick(1) }
        );
        assert_eq!(
            ctl_ok(addr, &["step", "3"]),
            BusResponse::Stepped { tick: Tick(4) }
        );
        assert_eq!(server.bank().tick(), Tick(4));

        // A write after the steps stamps the bank's new tick.
        assert_eq!(
            ctl_ok(addr, &["write", "4", "5.0"]),
            BusResponse::Written { tick: Tick(4) }
        );
        assert_eq!(
            ctl_ok(addr, &["read", "4"]),
            BusResponse::Sample {
                sample: Sample::good(Value::Float(5.0), Tick(4))
            }
        );
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
fn server_error_answers_exit_nonzero_naming_the_error() {
    with_server(&fixture_decls(), |_, addr| {
        // A register the device does not serve.
        let output = ctl(addr, &["read", "99"]);
        assert!(!output.status.success());
        let text = stderr(&output);
        assert!(text.contains("no register 99"), "{text}");

        // The write path's declared-kind lookup reports the same before
        // any write.
        let output = ctl(addr, &["write", "99", "1"]);
        assert!(!output.status.success());
        let text = stderr(&output);
        assert!(text.contains("no register 99"), "{text}");

        // A literal of another kind is still sent, and the server names
        // the kind mismatch.
        let output = ctl(addr, &["write", "4", "true"]);
        assert!(!output.status.success());
        let text = stderr(&output);
        assert!(
            text.contains("register 4") && text.contains("Float") && text.contains("Bool"),
            "{text}"
        );

        // A field-mutating request while another attachment holds the
        // write claim answers fenced — reads stay open.
        let holder = BusDriver::connect(
            addr,
            &[PointRegister {
                point: PointId(1),
                register: 4,
                kind: ValueKind::Float,
            }],
        )
        .unwrap();
        holder.claim_writer(7).unwrap();
        let output = ctl(addr, &["step"]);
        assert!(!output.status.success());
        let text = stderr(&output);
        assert!(text.contains("fenced"), "{text}");
        let output = ctl(addr, &["write", "4", "1.0"]);
        assert!(!output.status.success());
        let text = stderr(&output);
        assert!(text.contains("fenced"), "{text}");
        assert!(ctl(addr, &["read", "4"]).status.success());
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
        vec![dead, "read", "70000"],
        vec![dead, "read", "-1"],
        vec![dead, "read", "4", "extra"],
        vec![dead, "write", "4"],
        vec![dead, "write", "abc", "1"],
        vec![dead, "write", "4", "abc"],
        vec![dead, "write", "4", "nan"],
        vec![dead, "write", "4", "1", "extra"],
        vec![dead, "step", "0"],
        vec![dead, "step", "-1"],
        vec![dead, "step", "1.5"],
        vec![dead, "step", "abc"],
        vec![dead, "step", "1", "extra"],
    ];
    for args in &cases {
        let output = ctl_args(args);
        assert!(!output.status.success(), "{args:?} unexpectedly succeeded");
        let stderr = stderr(&output);
        assert!(stderr.contains("usage:"), "{args:?}: {stderr}");
        assert!(!stderr.contains("panic"), "{args:?}: {stderr}");
    }
}

#[test]
fn identical_request_sequences_produce_identical_output() {
    let script = |addr: SocketAddr| -> Vec<(bool, String)> {
        [
            vec!["list"],
            vec!["read", "4"],
            vec!["write", "4", "2.5"],
            vec!["read", "4"],
            vec!["step"],
            vec!["write", "9", "-3"],
            vec!["write", "7", "true"],
            vec!["step", "2"],
            vec!["list"],
            vec!["read", "9"],
        ]
        .iter()
        .map(|args| {
            let output = ctl(addr, args);
            (
                output.status.success(),
                String::from_utf8_lossy(&output.stdout).into_owned(),
            )
        })
        .collect()
    };
    let first = with_server(&fixture_decls(), |_, addr| script(addr));
    let second = with_server(&fixture_decls(), |_, addr| script(addr));
    assert!(first.iter().all(|(ok, _)| *ok));
    assert_eq!(first, second);
}
