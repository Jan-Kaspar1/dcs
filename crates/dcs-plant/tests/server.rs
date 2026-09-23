//! Process-level integration tests for `dcs-plant-server`: the binary
//! spawned against the checked-in tank-loop fixture pair, two attached
//! `RemoteDriver`s sharing the stepped plant, stepping on explicit
//! request only, the named failure surface, graceful shutdown, the
//! standalone `--check-dynamics` preflight, and identical scripted runs
//! across restarts.

use dcs_core::{IoDriver, IoError, PointId, Sample, Value};
use dcs_sim_net::RemoteDriver;
use std::io::{BufRead, BufReader, Read};
use std::net::SocketAddr;
use std::net::TcpListener;
use std::process::{Child, ChildStderr, Command, ExitStatus, Output, Stdio};

/// The binary under test, built by Cargo alongside the test harness.
const SERVER: &str = env!("CARGO_BIN_EXE_dcs-plant-server");
const MODEL: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/fixtures/tank_loop.json");
const DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/fixtures/tank_loop_dynamics.json"
);
const SECOND_ORDER_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/fixtures/tank_loop_second_order_dynamics.json"
);
const NOISE_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/fixtures/tank_loop_noise_dynamics.json"
);
const STATION_MODEL: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/fixtures/pump_station.json");
const STATION_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-sim/fixtures/pump_station_dynamics.json"
);
const DOSING_MODEL: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/fixtures/dosing_skid.json");
const DOSING_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-sim/fixtures/dosing_skid_dynamics.json"
);
const PROTECTION_MODEL: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/fixtures/protection.json");
const PROTECTION_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-sim/fixtures/protection_dynamics.json"
);
const UNBOUND_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/fixtures/invalid/dynamics_unbound_point.json"
);
const MALFORMED_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/fixtures/invalid/dynamics_malformed.json"
);
const UNBOUND_CONFLICTING_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/fixtures/invalid/dynamics_unbound_and_conflicting.json"
);
const UNKNOWN_DEVICE_MODEL: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-assembly/fixtures/invalid/unknown_device_kind.json"
);
const INVALID_MODEL: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-model/fixtures/invalid/duplicate_id.json"
);

/// A running `dcs-plant-server` child: its process, bound address, and
/// stderr stream (held open so the child's later messages never meet a
/// closed pipe).
struct Plant {
    child: Child,
    addr: SocketAddr,
    stderr: BufReader<ChildStderr>,
}

/// Spawns the server with `args` and reads its `listening on` line to
/// learn the bound address — `--listen 127.0.0.1:0` binds an ephemeral
/// port with no reservation race.
fn spawn(args: &[&str]) -> Plant {
    let mut child = Command::new(SERVER)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .expect("dcs-plant-server spawns");
    let mut stderr = BufReader::new(child.stderr.take().unwrap());
    let mut line = String::new();
    if stderr.read_line(&mut line).unwrap() == 0 {
        // The process exited before announcing its address; report why.
        let status = child.wait().unwrap();
        let mut rest = String::new();
        stderr.read_to_string(&mut rest).ok();
        panic!("dcs-plant-server exited {status} before serving: {rest}");
    }
    let addr = line
        .trim()
        .strip_prefix("listening on ")
        .unwrap_or_else(|| panic!("unexpected server output: {line:?}"))
        .parse()
        .expect("the announced address parses");
    Plant {
        child,
        addr,
        stderr,
    }
}

/// Signals the child with SIGTERM and waits for it to exit; a graceful
/// shutdown reports `success`. Drains the child's stderr afterward so a
/// failing wait can be diagnosed.
fn stop(plant: &mut Plant) -> ExitStatus {
    let status = Command::new("kill")
        .args(["-TERM", &plant.child.id().to_string()])
        .status()
        .expect("kill runs");
    assert!(status.success(), "kill could not signal the child");
    let status = plant.child.wait().unwrap();
    if !status.success() {
        let mut rest = String::new();
        plant.stderr.read_to_string(&mut rest).ok();
        panic!("dcs-plant-server exited {status}, stderr: {rest}");
    }
    status
}

/// Runs the binary expecting failure and returns its output.
fn run_fail(args: &[&str]) -> Output {
    let output = Command::new(SERVER)
        .args(args)
        .output()
        .expect("dcs-plant-server runs");
    assert!(!output.status.success(), "{args:?} unexpectedly succeeded");
    output
}

fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

#[test]
fn two_attached_drivers_observe_the_same_stepped_state() {
    let mut plant = spawn(&[MODEL, "--dynamics", DYNAMICS, "--listen", "127.0.0.1:0"]);
    let active = RemoteDriver::connect(plant.addr).unwrap();
    let standby = RemoteDriver::connect(plant.addr).unwrap();
    // The driving attachment owns the field's write claim — the
    // fail-closed field refuses a claim-less mutation.
    active.claim_writer(1).unwrap();

    // The lag seeds its output — the raw tank level — at its declared
    // initial value, before any step.
    assert_eq!(active.read(PointId(10)).unwrap().value, Value::Float(4.0));

    active.write(PointId(20), Value::Float(12.0)).unwrap();
    let tick = active.step(0.1).unwrap();

    // The lag's exact discretization y += (1 - e^{-dt/τ})(u - y), as
    // SimDriver computes it.
    let expected = 4.0 + (1.0 - (-0.1_f64 / 2.0).exp()) * (12.0 - 4.0);
    let seen_by_active = active.read(PointId(10)).unwrap();
    let seen_by_standby = standby.read(PointId(10)).unwrap();
    assert_eq!(seen_by_active, seen_by_standby);
    assert_eq!(seen_by_active.value, Value::Float(expected));
    assert_eq!(seen_by_active.tick, tick);

    assert!(stop(&mut plant).success());
}

#[test]
fn a_declared_lag_advances_only_on_explicit_step_requests() {
    let mut plant = spawn(&[MODEL, "--dynamics", DYNAMICS, "--listen", "127.0.0.1:0"]);
    let driver = RemoteDriver::connect(plant.addr).unwrap();
    driver.claim_writer(1).unwrap();

    // Reads and writes alone never advance the plant: the tick and the
    // lag's output hold until a step request arrives.
    let before = driver.read(PointId(10)).unwrap();
    driver.write(PointId(20), Value::Float(16.0)).unwrap();
    assert_eq!(driver.read(PointId(10)).unwrap(), before);

    let tick = driver.step(0.5).unwrap();
    let after = driver.read(PointId(10)).unwrap();
    assert_eq!(after.tick, tick);
    let (Value::Float(start), Value::Float(level)) = (before.value, after.value) else {
        panic!("lag points are Float")
    };
    assert!(
        level > start,
        "a step toward input 16.0 raised the level: {start} -> {level}"
    );
    // ...and no further: the stepped value holds across reads.
    assert_eq!(driver.read(PointId(10)).unwrap(), after);

    assert!(stop(&mut plant).success());
}

#[test]
fn a_declared_second_order_lag_loads_and_overshoots_its_step_input() {
    let mut plant = spawn(&[
        MODEL,
        "--dynamics",
        SECOND_ORDER_DYNAMICS,
        "--listen",
        "127.0.0.1:0",
    ]);
    let driver = RemoteDriver::connect(plant.addr).unwrap();
    driver.claim_writer(1).unwrap();

    // The element seeds its output — the raw tank level — at its
    // declared initial value.
    assert_eq!(driver.read(PointId(10)).unwrap().value, Value::Float(4.0));

    driver.write(PointId(20), Value::Float(12.0)).unwrap();
    // τ = 1.0, ζ = 0.25: the continuous-time response to the 8-unit
    // step change, evaluated at each tick boundary — what the element's
    // exact zero-order-hold discretization reproduces.
    let sigma = 0.25;
    let wd = (1.0_f64 - 0.25 * 0.25).sqrt();
    let expected =
        |t: f64| 12.0 - 8.0 * (-sigma * t).exp() * ((wd * t).cos() + sigma / wd * (wd * t).sin());
    let dt = 0.25;
    let mut peak = f64::MIN;
    for tick in 1..=60_u64 {
        driver.step(dt).unwrap();
        let Value::Float(level) = driver.read(PointId(10)).unwrap().value else {
            panic!("the driven point is Float")
        };
        peak = peak.max(level);
        let wanted = expected(tick as f64 * dt);
        assert!(
            (level - wanted).abs() < 1e-9,
            "tick {tick}: level={level} expected={wanted}"
        );
    }
    // The underdamped element overshot its input, then settled to it.
    assert!(peak > 15.0, "peak {peak}");
    let Value::Float(level) = driver.read(PointId(10)).unwrap().value else {
        panic!("the driven point is Float")
    };
    assert!((level - 12.0).abs() < 0.25, "settled level {level}");

    assert!(stop(&mut plant).success());
}

#[test]
fn a_declared_noise_element_loads_and_deviates_within_amplitude() {
    // The document cascades the tank's lag into a noise element: point
    // 11 follows the lag's driven level on point 10 plus a seeded
    // deviation bounded by the declared amplitude. One scripted pass
    // records the `(level, noisy)` pair each step — the sequence a
    // restarted server must reproduce bit-for-bit.
    let script = |addr: SocketAddr| -> Vec<(f64, f64)> {
        let driver = RemoteDriver::connect(addr).unwrap();
        driver.claim_writer(1).unwrap();
        // The element seeds its output at its declared initial before
        // any step.
        assert_eq!(driver.read(PointId(11)).unwrap().value, Value::Float(4.0));
        driver.write(PointId(20), Value::Float(12.0)).unwrap();
        (0..20)
            .map(|_| {
                driver.step(0.1).unwrap();
                let (Value::Float(level), Value::Float(noisy)) = (
                    driver.read(PointId(10)).unwrap().value,
                    driver.read(PointId(11)).unwrap().value,
                ) else {
                    panic!("the driven points are Float")
                };
                (level, noisy)
            })
            .collect()
    };

    let args = [
        MODEL,
        "--dynamics",
        NOISE_DYNAMICS,
        "--listen",
        "127.0.0.1:0",
    ];
    let mut first = spawn(&args);
    let first_run = script(first.addr);
    assert!(stop(&mut first).success());

    let mut second = spawn(&args);
    let second_run = script(second.addr);
    assert!(stop(&mut second).success());

    // The seeded generator replays identically across restarts.
    assert_eq!(first_run, second_run);
    for (step, (level, noisy)) in first_run.iter().enumerate() {
        // The documented bound: every draw stays within u ± amplitude.
        assert!(
            (noisy - level).abs() <= 0.5,
            "step {step}: noisy={noisy} outside level={level} ± 0.5"
        );
    }
    // The deviation is real — the element is not a passthrough.
    assert!(first_run.iter().any(|(level, noisy)| noisy != level));
}

#[test]
fn a_pump_command_drains_the_well_only_while_it_stands() {
    // The station loop the vocabulary was added for: two Bool-gated
    // pump draws and the declared inflow summed into an integrator
    // driving the level point.
    let mut plant = spawn(&[
        STATION_MODEL,
        "--dynamics",
        STATION_DYNAMICS,
        "--listen",
        "127.0.0.1:0",
    ]);
    let driver = RemoteDriver::connect(plant.addr).unwrap();
    driver.claim_writer(1).unwrap();
    let level = |driver: &RemoteDriver| {
        let Value::Float(level) = driver.read(PointId(10)).unwrap().value else {
            panic!("the level point is Float")
        };
        level
    };

    // The integrator seeds the well at its declared initial level.
    assert_eq!(level(&driver), 50.0);
    // Idle, the declared inflow alone — net +4 — climbs the well.
    driver.step(1.0).unwrap();
    assert_eq!(level(&driver), 54.0);

    // Asserting pump 1's run command drains at the declared draw —
    // net 4 - 10 = -6 — each step the command stands.
    driver.write(PointId(20), Value::Bool(true)).unwrap();
    driver.step(1.0).unwrap();
    assert_eq!(level(&driver), 48.0);
    driver.step(1.0).unwrap();
    assert_eq!(level(&driver), 42.0);

    // A second running pump doubles the draw.
    driver.write(PointId(21), Value::Bool(true)).unwrap();
    driver.step(1.0).unwrap();
    assert_eq!(level(&driver), 26.0);

    // Releasing both commands stops the draw at the next tick
    // boundary; the well climbs on inflow alone again.
    driver.write(PointId(20), Value::Bool(false)).unwrap();
    driver.write(PointId(21), Value::Bool(false)).unwrap();
    driver.step(1.0).unwrap();
    assert_eq!(level(&driver), 30.0);

    assert!(stop(&mut plant).success());
}

/// One scripted pass over the dosing loop: the level and measured
/// discharge rate each step while the analog demand stands at 50,
/// then 20, then 0 — the trace identical runs must reproduce.
fn dosing_script(addr: SocketAddr) -> Vec<Sample> {
    let driver = RemoteDriver::connect(addr).unwrap();
    driver.claim_writer(1).unwrap();
    let mut trace = Vec::new();
    for demand in [50.0, 20.0, 0.0] {
        driver.write(PointId(20), Value::Float(demand)).unwrap();
        for _ in 0..2 {
            driver.step(1.0).unwrap();
            trace.push(driver.read(PointId(10)).unwrap());
            trace.push(driver.read(PointId(11)).unwrap());
        }
    }
    trace
}

#[test]
fn an_analog_demand_drains_the_tank_proportionally_through_the_merge() {
    // The dosing loop the scaled_flow element exists for: the
    // metering pump's analog speed demand scaled into the measured
    // discharge rate and, with a negative gain, into the chemical
    // tank's drawdown — exercised end to end through the `--dynamics`
    // merge.
    let args = [
        DOSING_MODEL,
        "--dynamics",
        DOSING_DYNAMICS,
        "--listen",
        "127.0.0.1:0",
    ];
    let mut plant = spawn(&args);
    let driver = RemoteDriver::connect(plant.addr).unwrap();
    driver.claim_writer(1).unwrap();
    let read = |point: u64| {
        let Value::Float(value) = driver.read(PointId(point)).unwrap().value else {
            panic!("the skid's points are Float")
        };
        value
    };

    // The integrator seeds the tank at its declared initial level and
    // the zeroed demand drives zero rates — the tank holds.
    assert_eq!(read(10), 100.0);
    driver.step(1.0).unwrap();
    assert_eq!(read(10), 100.0);
    assert_eq!(read(11), 0.0);

    // The demand standing at 50, the discharge reads 0.5 × 50 and the
    // tank draws down 25 per time unit, step after step.
    driver.write(PointId(20), Value::Float(50.0)).unwrap();
    driver.step(1.0).unwrap();
    assert_eq!(read(11), 25.0);
    assert_eq!(read(10), 75.0);
    driver.step(1.0).unwrap();
    assert_eq!(read(10), 50.0);

    // A changed demand re-scales the draw at the next tick boundary;
    // zeroing it stops the draw and the level holds.
    driver.write(PointId(20), Value::Float(20.0)).unwrap();
    driver.step(1.0).unwrap();
    assert_eq!(read(11), 10.0);
    assert_eq!(read(10), 40.0);
    driver.write(PointId(20), Value::Float(0.0)).unwrap();
    driver.step(1.0).unwrap();
    assert_eq!(read(10), 40.0);

    assert!(stop(&mut plant).success());

    // Identical scripted step sequences produce identical point
    // traces across a restart of the merged plant.
    let mut first = spawn(&args);
    let first_run = dosing_script(first.addr);
    assert!(stop(&mut first).success());
    let mut second = spawn(&args);
    let second_run = dosing_script(second.addr);
    assert!(stop(&mut second).success());
    assert_eq!(first_run, second_run);
}

/// One scripted pass over the protection loop: the level and the
/// `sis-active` contact each step — the trace identical runs must
/// reproduce.
fn protection_script(addr: SocketAddr) -> Vec<(Sample, Sample)> {
    let driver = RemoteDriver::connect(addr).unwrap();
    driver.claim_writer(1).unwrap();
    (0..6)
        .map(|_| {
            driver.step(1.0).unwrap();
            (
                driver.read(PointId(10)).unwrap(),
                driver.read(PointId(30)).unwrap(),
            )
        })
        .collect()
}

#[test]
fn the_level_crossing_drives_the_protection_contact_through_the_merge() {
    // The threshold element the vocabulary was missing: a Float input
    // driving a Bool contact with a declared hysteresis band, merged
    // through `--dynamics` and gating a `bool_flow` emergency draw —
    // a plant-side protection pattern expressed entirely in a
    // dynamics document, no scheduled script asserting the contact.
    let args = [
        PROTECTION_MODEL,
        "--dynamics",
        PROTECTION_DYNAMICS,
        "--listen",
        "127.0.0.1:0",
    ];
    let mut plant = spawn(&args);
    let driver = RemoteDriver::connect(plant.addr).unwrap();
    driver.claim_writer(1).unwrap();
    let level = || {
        let Value::Float(level) = driver.read(PointId(10)).unwrap().value else {
            panic!("the level point is Float")
        };
        level
    };
    let contact = || driver.read(PointId(30)).unwrap().value;

    // The integrator seeds the well at its declared initial level and
    // the threshold seeds the contact at its declared initial Bool.
    assert_eq!(level(), 6.0);
    assert_eq!(contact(), Value::Bool(false));

    // The declared inflow — net +4 per unit — climbs the well to 10,
    // past the `on` bound of 8: the next step's threshold read
    // asserts the contact and engages the draw.
    driver.step(1.0).unwrap();
    assert_eq!(level(), 10.0);
    assert_eq!(contact(), Value::Bool(false));
    driver.step(1.0).unwrap();
    assert_eq!(contact(), Value::Bool(true));
    assert_eq!(driver.read(PointId(12)).unwrap().value, Value::Float(-20.0));
    assert_eq!(level(), -6.0);

    // Back below `off`, the contact releases and the draw stops at
    // the next boundary — the loop oscillates on the element's own
    // bounds with no script tick asserting anything.
    driver.step(1.0).unwrap();
    assert_eq!(contact(), Value::Bool(false));
    assert_eq!(driver.read(PointId(12)).unwrap().value, Value::Float(0.0));
    assert_eq!(level(), -2.0);

    assert!(stop(&mut plant).success());

    // Identical scripted step sequences produce identical point
    // traces across a restart of the merged plant.
    let mut first = spawn(&args);
    let first_run = protection_script(first.addr);
    assert!(stop(&mut first).success());
    let mut second = spawn(&args);
    let second_run = protection_script(second.addr);
    assert!(stop(&mut second).success());
    assert_eq!(first_run, second_run);
    // The trace actually asserted and released the contact.
    assert!(
        first_run
            .iter()
            .any(|(_, contact)| contact.value == Value::Bool(true))
    );
    assert!(
        first_run
            .iter()
            .any(|(_, contact)| contact.value == Value::Bool(false))
    );
}

#[test]
fn check_dynamics_accepts_a_valid_document_without_serving() {
    // The standalone preflight: a valid document exits zero with the
    // element summary on stdout — and since no listener binds, stderr
    // stays silent and the process exits on its own, no signal needed.
    let output = Command::new(SERVER)
        .args([MODEL, "--check-dynamics", DYNAMICS])
        .output()
        .expect("dcs-plant-server runs");
    assert!(output.status.success(), "{}", stderr(&output));
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("check ok"), "{stdout}");
    assert!(stdout.contains("elements: 1"), "{stdout}");
    assert!(stdout.contains("first_order_lag: 1"), "{stdout}");
    assert!(output.stderr.is_empty(), "{}", stderr(&output));
}

#[test]
fn check_dynamics_rejects_invalid_documents_without_serving() {
    // An element driving a point the model does not bind is named by
    // index and driving point — the same message `--dynamics` reports
    // at startup.
    let output = run_fail(&[MODEL, "--check-dynamics", UNBOUND_DYNAMICS]);
    let message = stderr(&output);
    assert!(message.contains("dynamics element 0"), "{message}");
    assert!(message.contains("99"), "{message}");
    assert!(!message.contains("listening on"), "{message}");

    // A document with several malformed elements reports each rejection
    // in one pass: indices 0 and 2 drive unbound points, index 3
    // conflicts with the point the valid element 1 merged onto, and
    // element 1 itself is not named.
    let output = run_fail(&[MODEL, "--check-dynamics", UNBOUND_CONFLICTING_DYNAMICS]);
    let message = stderr(&output);
    for (index, point) in [(0, 98), (2, 97), (3, 10)] {
        assert!(
            message.contains(&format!("dynamics element {index} (driving point {point})")),
            "{message}"
        );
    }
    assert!(!message.contains("dynamics element 1"), "{message}");

    // A document that is not a process-element list at all.
    let output = run_fail(&[MODEL, "--check-dynamics", MALFORMED_DYNAMICS]);
    let message = stderr(&output);
    assert!(message.contains("dynamics"), "{message}");
    assert!(message.contains("dynamics_malformed.json"), "{message}");

    // A dynamics path that does not exist names the path.
    let output = run_fail(&[MODEL, "--check-dynamics", "no-such-dynamics.json"]);
    assert!(stderr(&output).contains("no-such-dynamics.json"));

    // A model failing validation reports before any element is read.
    let output = run_fail(&[INVALID_MODEL, "--check-dynamics", DYNAMICS]);
    let message = stderr(&output);
    assert!(message.contains("invalid plant model"), "{message}");

    // The serve-mode options do not combine with the preflight: each is
    // rejected as a usage error, naming the refused flag.
    for flag in ["--listen", "--dynamics"] {
        let output = Command::new(SERVER)
            .args([MODEL, "--check-dynamics", DYNAMICS, flag, "x"])
            .output()
            .expect("dcs-plant-server runs");
        assert_eq!(output.status.code(), Some(2), "{flag}");
        let message = stderr(&output);
        assert!(message.contains("--check-dynamics"), "{message}");
        assert!(message.contains(flag), "{message}");
    }

    // The flag requires its document argument.
    let output = Command::new(SERVER)
        .args([MODEL, "--check-dynamics"])
        .output()
        .expect("dcs-plant-server runs");
    assert_eq!(output.status.code(), Some(2));
    assert!(stderr(&output).contains("--check-dynamics"));
}

#[test]
fn malformed_dynamics_exit_nonzero_naming_the_element() {
    // A process element driving a point the model does not bind.
    let output = run_fail(&[
        MODEL,
        "--dynamics",
        UNBOUND_DYNAMICS,
        "--listen",
        "127.0.0.1:0",
    ]);
    let message = stderr(&output);
    assert!(message.contains("dynamics element 0"), "{message}");
    assert!(message.contains("99"), "{message}");

    // A document that is not a process-element list at all.
    let output = run_fail(&[
        MODEL,
        "--dynamics",
        MALFORMED_DYNAMICS,
        "--listen",
        "127.0.0.1:0",
    ]);
    let message = stderr(&output);
    assert!(message.contains("dynamics"), "{message}");
    assert!(message.contains("dynamics_malformed.json"), "{message}");

    // A dynamics path that does not exist.
    let output = run_fail(&[
        MODEL,
        "--dynamics",
        "no-such-dynamics.json",
        "--listen",
        "127.0.0.1:0",
    ]);
    assert!(stderr(&output).contains("no-such-dynamics.json"));
}

#[test]
fn unknown_model_inputs_exit_nonzero_naming_the_element() {
    // A model path that does not exist names the path.
    let output = run_fail(&["no-such-model.json", "--listen", "127.0.0.1:0"]);
    assert!(stderr(&output).contains("no-such-model.json"));

    // A document failing model validation names the offending element.
    let output = run_fail(&[INVALID_MODEL, "--listen", "127.0.0.1:0"]);
    let message = stderr(&output);
    assert!(message.contains("invalid plant model"), "{message}");
    assert!(message.contains("10"), "{message}");

    // A device kind the simulated plant cannot serve names the device
    // and its kind.
    let output = run_fail(&[UNKNOWN_DEVICE_MODEL, "--listen", "127.0.0.1:0"]);
    let message = stderr(&output);
    assert!(message.contains("device 1"), "{message}");
    assert!(message.contains("ethercat-8ai"), "{message}");
}

#[test]
fn an_occupied_listen_address_exits_nonzero_naming_it() {
    // Hold the address so the server's own bind must fail.
    let blocker = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let addr = blocker.local_addr().unwrap().to_string();
    let output = run_fail(&[MODEL, "--listen", &addr]);
    let stderr = stderr(&output);
    assert!(stderr.contains(&addr), "{stderr}");
}

#[test]
fn shutdown_is_graceful_and_the_port_refuses_connections() {
    let mut plant = spawn(&[MODEL, "--dynamics", DYNAMICS, "--listen", "127.0.0.1:0"]);
    let driver = RemoteDriver::connect(plant.addr).unwrap();
    assert!(driver.read(PointId(10)).is_ok());

    // SIGTERM ends the run with a clean exit, not a signal status.
    assert!(stop(&mut plant).success());

    // The bound address refuses new connections...
    let error = RemoteDriver::connect(plant.addr).unwrap_err();
    assert_eq!(error.kind(), std::io::ErrorKind::ConnectionRefused);
    // ...and the already-attached client sees the plant go away rather
    // than hang.
    assert_eq!(
        driver.read(PointId(10)),
        Err(IoError::Disconnected(PointId(10)))
    );
}

#[test]
fn identical_request_sequences_produce_identical_responses_across_restarts() {
    // One scripted pass over a fresh plant: the serialized results are
    // the responses a restart must reproduce bit-for-bit.
    let script = |addr: SocketAddr| -> Vec<serde_json::Value> {
        let driver = RemoteDriver::connect(addr).unwrap();
        driver.claim_writer(1).unwrap();
        let mut responses = Vec::new();
        responses.push(serde_json::to_value(driver.read(PointId(10)).unwrap()).unwrap());
        driver.write(PointId(20), Value::Float(12.0)).unwrap();
        responses.push(serde_json::to_value(driver.step(0.1).unwrap()).unwrap());
        responses.push(serde_json::to_value(driver.read(PointId(10)).unwrap()).unwrap());
        responses.push(serde_json::to_value(driver.step(0.1).unwrap()).unwrap());
        responses.push(serde_json::to_value(driver.read(PointId(10)).unwrap()).unwrap());
        responses.push(serde_json::to_value(driver.list_points().unwrap()).unwrap());
        responses
    };

    let args = [MODEL, "--dynamics", DYNAMICS, "--listen", "127.0.0.1:0"];
    let mut first = spawn(&args);
    let first_run = script(first.addr);
    assert!(stop(&mut first).success());

    let mut second = spawn(&args);
    let second_run = script(second.addr);
    assert!(stop(&mut second).success());

    assert_eq!(first_run, second_run);
}
