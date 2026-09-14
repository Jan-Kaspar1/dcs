//! CLI tests for the `dcs-controller` binary: deterministic `--ticks`
//! output and nonzero exits naming the failing element.

use std::process::Command;

const BINARY: &str = env!("CARGO_BIN_EXE_dcs-controller");
const TANK_LOOP: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-assembly/fixtures/tank_loop.json"
);
const UNKNOWN_KIND: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-assembly/fixtures/invalid/unknown_component_kind.json"
);

fn run(args: &[&str]) -> std::process::Output {
    Command::new(BINARY).args(args).output().unwrap()
}

#[test]
fn two_ticks_runs_produce_identical_snapshot_output() {
    let first = run(&[TANK_LOOP, "--ticks", "50"]);
    let second = run(&[TANK_LOOP, "--ticks", "50"]);

    assert!(first.status.success());
    assert!(second.status.success());
    assert_eq!(first.stdout, second.stdout);

    let stdout = String::from_utf8(first.stdout).unwrap();
    let snapshot: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    assert_eq!(snapshot["tick"], 50);
    assert_eq!(snapshot["points"].as_array().unwrap().len(), 5);
}

#[test]
fn assembly_error_exits_nonzero_and_names_the_element() {
    let output = run(&[UNKNOWN_KIND, "--ticks", "10"]);
    assert!(!output.status.success());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(
        stderr.contains("component 1") && stderr.contains("\"flux-capacitor\""),
        "{stderr}"
    );
}

#[test]
fn load_errors_exit_nonzero() {
    let output = run(&["does-not-exist.json", "--ticks", "10"]);
    assert!(!output.status.success());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains("does-not-exist.json"), "{stderr}");

    // A checked-in document that parses but fails model validation.
    const INVALID_MODEL: &str = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../dcs-model/fixtures/invalid/duplicate_id.json"
    );
    let output = run(&[INVALID_MODEL, "--ticks", "10"]);
    assert!(!output.status.success());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains("invalid plant model"), "{stderr}");
}

#[test]
fn missing_model_file_is_a_usage_error() {
    let output = run(&[]);
    assert_eq!(output.status.code(), Some(2));
}
