//! End-to-end tests for the `dcs-model` command-line tool against the
//! checked-in fixtures.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

const BIN: &str = env!("CARGO_BIN_EXE_dcs-model");

fn fixture(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("fixtures")
        .join(name)
}

fn run(command: &str, file: &Path) -> Output {
    Command::new(BIN)
        .arg(command)
        .arg(file)
        .output()
        .expect("failed to run dcs-model")
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).unwrap()
}

fn stderr(output: &Output) -> String {
    String::from_utf8(output.stderr.clone()).unwrap()
}

#[test]
fn validate_accepts_valid_fixtures() {
    for name in ["minimal.json", "signal_index.json", "signal_groups.json"] {
        let output = run("validate", &fixture(name));
        assert!(
            output.status.success(),
            "{name} rejected: {}",
            stderr(&output)
        );
    }
}

#[test]
fn validate_rejects_each_invalid_fixture_with_its_error() {
    let cases: [(&str, &str); 6] = [
        ("duplicate_id.json", "duplicate io_point id 10"),
        (
            "dangling_reference.json",
            "connection 0 from end names unknown io point 99",
        ),
        (
            "dangling_signal.json",
            "signal 100 sources unknown io point 99",
        ),
        ("direction_mismatch.json", "cannot produce a value"),
        (
            "internal_missing_initial.json",
            "internal io point 11 declares no initial value",
        ),
        (
            "unknown_channel.json",
            "io point 10 binds unknown channel \"ch9\" on device 1",
        ),
    ];
    for (name, expected) in cases {
        let output = run("validate", &fixture("invalid").join(name));
        assert!(!output.status.success(), "{name} unexpectedly validated");
        let stderr = stderr(&output);
        assert!(stderr.contains(expected), "{name}: {stderr}");
    }
}

#[test]
fn summary_prints_element_counts() {
    let output = run("summary", &fixture("minimal.json"));
    assert!(output.status.success(), "{}", stderr(&output));
    let stdout = stdout(&output);
    for line in [
        "devices: 2",
        "io_points: 2",
        "signals: 1",
        "signal_groups: 0",
        "components: 1",
        "connections: 2",
    ] {
        assert!(stdout.contains(line), "{line} missing from:\n{stdout}");
    }
}

#[test]
fn summary_counts_declared_signal_groups() {
    // The fixture declares two distinct groups across four signals; the
    // ungrouped signal adds none.
    let output = run("summary", &fixture("signal_groups.json"));
    assert!(output.status.success(), "{}", stderr(&output));
    let stdout = stdout(&output);
    for line in ["signals: 4", "signal_groups: 2"] {
        assert!(stdout.contains(line), "{line} missing from:\n{stdout}");
    }
}

#[test]
fn signal_index_matches_checked_in_output() {
    let output = run("signal-index", &fixture("signal_index.json"));
    assert!(output.status.success(), "{}", stderr(&output));
    let expected = include_str!("../fixtures/signal_index.index.json");
    assert_eq!(stdout(&output).trim_end(), expected.trim_end());
}

#[test]
fn signal_index_rejects_an_invalid_model() {
    let output = run(
        "signal-index",
        &fixture("invalid").join("unknown_channel.json"),
    );
    assert!(!output.status.success());
    assert!(stderr(&output).contains("validation error"));
}

#[test]
fn malformed_input_reports_the_problem_without_panicking() {
    let path = std::env::temp_dir().join(format!("dcs-model-cli-test-{}.json", std::process::id()));
    std::fs::write(&path, "{ not json").unwrap();
    let output = run("validate", &path);
    let _ = std::fs::remove_file(&path);
    assert!(!output.status.success());
    assert!(
        stderr(&output).contains("malformed model document"),
        "{}",
        stderr(&output)
    );
}

#[test]
fn an_unreadable_file_reports_the_problem() {
    let output = run("validate", &fixture("does-not-exist.json"));
    assert!(!output.status.success());
    assert!(stderr(&output).contains("cannot read file"));
}

#[test]
fn no_arguments_prints_usage() {
    let output = Command::new(BIN).output().expect("failed to run dcs-model");
    assert!(!output.status.success());
    assert!(stderr(&output).contains("usage: dcs-model"));
}

#[test]
fn an_unknown_command_prints_usage() {
    let output = Command::new(BIN)
        .arg("frobnicate")
        .arg(fixture("minimal.json"))
        .output()
        .expect("failed to run dcs-model");
    assert!(!output.status.success());
    assert!(stderr(&output).contains("usage: dcs-model"));
}
