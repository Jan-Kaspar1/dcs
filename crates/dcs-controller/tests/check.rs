//! Tests for `dcs-controller --check`: the compile-check mode loads,
//! validates, and assembles a model through the standard registries,
//! prints what assembled, and exits — running no scan and binding no
//! listener. Every failure class exits nonzero naming the element,
//! exactly as a run would.

use std::io::Read;
use std::process::{Command, Output, Stdio};
use std::time::{Duration, Instant};

const BINARY: &str = env!("CARGO_BIN_EXE_dcs-controller");
const TANK_LOOP: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-assembly/fixtures/tank_loop.json"
);
const INVALID: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-assembly/fixtures/invalid"
);
const INVALID_MODEL: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../dcs-model/fixtures/invalid");

/// Runs the binary with `args`, failing rather than hanging when the
/// invocation enters the paced scan loop — a `--check` run performs no
/// scan and binds no listener, so it exits promptly on every path.
fn run(args: &[&str]) -> Output {
    let mut child = Command::new(BINARY)
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            let mut stdout = Vec::new();
            let mut stderr = Vec::new();
            child
                .stdout
                .take()
                .unwrap()
                .read_to_end(&mut stdout)
                .unwrap();
            child
                .stderr
                .take()
                .unwrap()
                .read_to_end(&mut stderr)
                .unwrap();
            return Output {
                status,
                stdout,
                stderr,
            };
        }
        if Instant::now() > deadline {
            child.kill().unwrap();
            panic!("dcs-controller {args:?} did not exit within 20s");
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).unwrap()
}

fn stderr(output: &Output) -> String {
    String::from_utf8(output.stderr.clone()).unwrap()
}

#[test]
fn check_on_a_valid_fixture_prints_the_assembly_summary_and_exits_zero() {
    let output = run(&[TANK_LOOP, "--check"]);
    assert!(output.status.success(), "{}", stderr(&output));
    let stdout = stdout(&output);
    // The tank-loop fixture declares one sim device, three io_points —
    // the port-to-port wire synthesizes two more served points — and two
    // components of distinct kinds.
    for line in [
        "check ok:",
        "devices: 1",
        "  sim: 1",
        "io_points: 3 declared, 5 served",
        "components: 2",
        "  analog-input: 1",
        "  pid: 1",
    ] {
        assert!(stdout.contains(line), "{line} missing from:\n{stdout}");
    }
    // No scan ran: the output is the summary, not a telemetry snapshot.
    assert!(!stdout.contains("\"tick\""), "{stdout}");
}

#[test]
fn check_output_is_deterministic_across_runs() {
    let first = run(&[TANK_LOOP, "--check"]);
    let second = run(&[TANK_LOOP, "--check"]);
    assert!(first.status.success());
    assert!(second.status.success());
    assert_eq!(first.stdout, second.stdout);
}

#[test]
fn check_names_an_unreadable_file() {
    let output = run(&["does-not-exist.json", "--check"]);
    assert!(!output.status.success());
    let stderr = stderr(&output);
    assert!(stderr.contains("does-not-exist.json"), "{stderr}");
}

#[test]
fn check_names_a_validation_failure() {
    let path = format!("{INVALID_MODEL}/duplicate_id.json");
    let output = run(&[&path, "--check"]);
    assert!(!output.status.success());
    let stderr = stderr(&output);
    assert!(
        stderr.contains("invalid plant model") && stderr.contains("duplicate io_point id 10"),
        "{stderr}"
    );
}

#[test]
fn check_names_a_malformed_document() {
    let path =
        std::env::temp_dir().join(format!("dcs-controller-check-{}.json", std::process::id()));
    std::fs::write(&path, "{ not json").unwrap();
    let output = run(&[path.to_str().unwrap(), "--check"]);
    let _ = std::fs::remove_file(&path);
    assert!(!output.status.success());
    assert!(
        stderr(&output).contains("malformed model document"),
        "{}",
        stderr(&output)
    );
}

#[test]
fn check_names_an_unknown_device_kind() {
    let path = format!("{INVALID}/unknown_device_kind.json");
    let output = run(&[&path, "--check"]);
    assert!(!output.status.success());
    let stderr = stderr(&output);
    assert!(
        stderr.contains("device 1") && stderr.contains("\"ethercat-8ai\""),
        "{stderr}"
    );
}

#[test]
fn check_names_an_unknown_component_kind() {
    let path = format!("{INVALID}/unknown_component_kind.json");
    let output = run(&[&path, "--check"]);
    assert!(!output.status.success());
    let stderr = stderr(&output);
    assert!(
        stderr.contains("component 1") && stderr.contains("\"flux-capacitor\""),
        "{stderr}"
    );
}

#[test]
fn check_names_a_port_binding_failure() {
    for (fixture, element) in [
        ("unbound_port.json", "port \"pv\""),
        ("port_bound_twice.json", "port \"pv\""),
    ] {
        let path = format!("{INVALID}/{fixture}");
        let output = run(&[&path, "--check"]);
        assert!(!output.status.success(), "{fixture} unexpectedly checked");
        let stderr = stderr(&output);
        assert!(
            stderr.contains("component 1") && stderr.contains(element),
            "{fixture}: {stderr}"
        );
    }
}

#[test]
fn check_names_a_parameter_failure() {
    // A component constructor rejecting the instance's parameters.
    let path = format!("{INVALID}/bad_parameters.json");
    let output = run(&[&path, "--check"]);
    assert!(!output.status.success());
    let message = stderr(&output);
    assert!(
        message.contains("component 1") && message.contains("\"kp\""),
        "{message}"
    );

    // A device-kind factory rejecting the device's parameters.
    let path = format!("{INVALID}/bad_device_parameters.json");
    let output = run(&[&path, "--check"]);
    assert!(!output.status.success());
    let message = stderr(&output);
    assert!(
        message.contains("device 1") && message.contains("\"address\""),
        "{message}"
    );
}

#[test]
fn check_rejects_run_mode_options_as_usage_errors() {
    // No listener can be requested and no pacing applies in check mode:
    // the run-mode options are rejected, not silently ignored.
    for extra in [
        vec!["--ticks", "5"],
        vec!["--scan-ms", "50"],
        vec!["--dt", "0.1"],
        vec!["--listen", "127.0.0.1:0"],
        vec!["--standby", "127.0.0.1:0"],
        vec!["--remote", "127.0.0.1:0"],
        vec!["--driven"],
        vec!["--auto-promote", "3"],
        vec!["--state-file", "/tmp/dcs-check-state.json"],
    ] {
        let mut args = vec![TANK_LOOP, "--check"];
        args.extend(extra.iter());
        let output = run(&args);
        assert_eq!(
            output.status.code(),
            Some(2),
            "{extra:?}: {}",
            stderr(&output)
        );
        assert!(
            stderr(&output).contains("--check"),
            "{extra:?}: {}",
            stderr(&output)
        );
    }
}

#[test]
fn check_without_a_model_file_is_a_usage_error() {
    let output = run(&["--check"]);
    assert_eq!(output.status.code(), Some(2));
}
