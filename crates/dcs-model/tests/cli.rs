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

fn run_diff(old: &Path, new: &Path, extra: &[&str]) -> Output {
    let mut command = Command::new(BIN);
    command.arg("diff").arg(old).arg(new).args(extra);
    command.output().expect("failed to run dcs-model")
}

fn run_lint(file: &Path, extra: &[&str]) -> Output {
    let mut command = Command::new(BIN);
    command.arg("lint").arg(file).args(extra);
    command.output().expect("failed to run dcs-model")
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

#[test]
fn diff_lists_added_removed_and_changed_elements_in_every_class() {
    let output = run_diff(
        &fixture("diff/base.json"),
        &fixture("diff/revised.json"),
        &[],
    );
    assert!(output.status.success(), "{}", stderr(&output));
    let stdout = stdout(&output);
    for line in [
        // devices: kind, channel, and membership changes
        "devices:",
        "  changed device 1",
        "    kind: \"sim-8ai\" -> \"sim-16ai\"",
        "    channels.ch1.value_type: \"float\" -> \"int\"",
        "    channels.ch2: added {\"direction\":\"in\",\"value_type\":\"float\"}",
        "  changed device 2",
        "    channels.ch1: added {\"direction\":\"out\",\"value_type\":\"float\"}",
        "  added device 3",
        "  removed device 9",
        // io_points: channel, direction, value-type, and initial changes
        "io_points:",
        "  changed io_point 10",
        "    direction: \"in\" -> \"out\"",
        "    channel.device: 1 -> 2",
        "    channel.name: \"ch0\" -> \"ch1\"",
        "  changed io_point 11",
        "    channel: removed {\"device\":2,\"name\":\"ch0\"}",
        "    initial: added {\"float\":0.0}",
        "  changed io_point 12",
        "    initial: {\"float\":0.0} -> {\"float\":3.5}",
        "  added io_point 13",
        "  changed io_point 14",
        "    value_type: \"int\" -> \"float\"",
        "    initial: {\"int\":0} -> {\"float\":1.0}",
        "  removed io_point 19",
        // signals
        "signals:",
        "  changed signal 100 \"reactor-inlet-temperature\"",
        "    name: \"reactor-temperature\" -> \"reactor-inlet-temperature\"",
        "    unit: \"degC\" -> \"degF\"",
        "    group: removed \"reactor\"",
        "    description: added \"first-pass outlet temperature\"",
        "  added signal 102 \"valve-command\"",
        "  removed signal 109 \"spare-output\"",
        // components: kind, parameter, and port-wiring changes
        "components:",
        "  changed component 1 \"pid\"",
        "    kind: \"gain\" -> \"pid\"",
        "    parameters.k: removed {\"float\":2.0}",
        "    parameters.kp: added {\"float\":1.5}",
        "    parameters.ti: added {\"float\":10.0}",
        "  changed component 2 \"first-order-lag\"",
        "    parameters.tau: {\"float\":5.0} -> {\"float\":8.0}",
        "    ports.enable: added {\"direction\":\"in\",\"value_type\":\"bool\"}",
        "    ports.out.value_type: \"float\" -> \"bool\"",
        "  added component 3 \"alarm\"",
        "  removed component 9 \"spare-gain\"",
        // connections
        "connections:",
        "  removed point 10 -> port \"in\" on component 1",
        "  removed port \"out\" on component 2 -> point 19",
        "  added port \"out\" on component 1 -> point 10",
        "  added point 12 -> port \"in\" on component 3",
        "  added port \"out\" on component 2 -> point 13",
    ] {
        assert!(stdout.contains(line), "{line} missing from:\n{stdout}");
    }
}

#[test]
fn diff_of_identical_documents_is_empty_and_exits_zero() {
    let output = run_diff(&fixture("diff/base.json"), &fixture("diff/base.json"), &[]);
    assert!(output.status.success(), "{}", stderr(&output));
    assert_eq!(stdout(&output).trim(), "no changes");
}

#[test]
fn diff_reports_an_invalid_document_with_its_validation_errors() {
    // The invalid document may sit on either side; each run names the
    // failing file and its validation errors instead of diffing.
    let invalid = fixture("invalid").join("unknown_channel.json");
    for (old, new, expected_path) in [
        (invalid.clone(), fixture("diff/base.json"), &invalid),
        (fixture("diff/base.json"), invalid.clone(), &invalid),
    ] {
        let output = run_diff(&old, &new, &[]);
        assert!(!output.status.success(), "diff unexpectedly succeeded");
        let stderr = stderr(&output);
        assert!(
            stderr.contains(&expected_path.display().to_string()),
            "stderr does not name the invalid document:\n{stderr}"
        );
        assert!(
            stderr.contains("io point 10 binds unknown channel \"ch9\" on device 1"),
            "stderr lacks the validation errors:\n{stderr}"
        );
    }
}

#[test]
fn diff_json_mode_parses_and_names_the_elements() {
    let output = run_diff(
        &fixture("diff/base.json"),
        &fixture("diff/revised.json"),
        &["--json"],
    );
    assert!(output.status.success(), "{}", stderr(&output));
    let diff: serde_json::Value = serde_json::from_str(&stdout(&output)).unwrap();
    let elements = |class: &str| -> Vec<&str> {
        diff[class]
            .as_array()
            .unwrap()
            .iter()
            .map(|entry| entry["element"].as_str().unwrap())
            .collect()
    };
    assert!(elements("devices").contains(&"device 3"));
    assert!(elements("io_points").contains(&"io_point 10"));
    assert!(elements("signals").contains(&"signal 102 \"valve-command\""));
    assert!(elements("components").contains(&"component 9 \"spare-gain\""));
    assert!(elements("connections").contains(&"port \"out\" on component 1 -> point 10"));
    // The change kind and field detail are machine-readable too.
    let device1 = diff["devices"]
        .as_array()
        .unwrap()
        .iter()
        .find(|entry| entry["element"] == "device 1")
        .unwrap();
    assert_eq!(device1["change"], "changed");
    assert_eq!(device1["id"], 1);
    assert!(
        device1["fields"]
            .as_array()
            .unwrap()
            .iter()
            .any(|field| field["field"] == "kind"
                && field["old"] == "sim-8ai"
                && field["new"] == "sim-16ai"),
        "{device1}"
    );
    // The empty diff serializes with every class present and empty.
    let output = run_diff(
        &fixture("diff/base.json"),
        &fixture("diff/base.json"),
        &["--json"],
    );
    assert!(output.status.success());
    let diff: serde_json::Value = serde_json::from_str(&stdout(&output)).unwrap();
    for class in [
        "devices",
        "io_points",
        "signals",
        "components",
        "connections",
    ] {
        assert_eq!(diff[class], serde_json::json!([]), "{class}");
    }
}

#[test]
fn diff_output_is_deterministic_across_runs() {
    for extra in [Vec::new(), vec!["--json"]] {
        let first = run_diff(
            &fixture("diff/base.json"),
            &fixture("diff/revised.json"),
            &extra,
        );
        let second = run_diff(
            &fixture("diff/base.json"),
            &fixture("diff/revised.json"),
            &extra,
        );
        assert_eq!(stdout(&first), stdout(&second));
    }
}

#[test]
fn diff_without_two_files_prints_usage() {
    let output = Command::new(BIN)
        .arg("diff")
        .arg(fixture("minimal.json"))
        .output()
        .expect("failed to run dcs-model");
    assert!(!output.status.success());
    assert!(stderr(&output).contains("usage: dcs-model"));
}

#[test]
fn lint_reports_each_finding_class_naming_its_element() {
    let output = run_lint(&fixture("lint/findings.json"), &[]);
    // Lint is advisory: findings print and the command still exits zero.
    assert!(output.status.success(), "{}", stderr(&output));
    let stdout = stdout(&output);
    for line in [
        "point_without_signal io_point 11: no signal sources this point; it renders unlabeled on the monitoring page",
        "signal_missing_unit signal 101 \"reactor-level-switch\": declares no unit",
        "signal_missing_group signal 101 \"reactor-level-switch\": declares no group",
        "signal_missing_description signal 102 \"level-setpoint\": declares no description",
        "writable_field_point io_point 10: writable field point bound to channel \"ch0\" on device 1 (operator surface)",
        "unbound_channel device 1 channel \"ch7\": is bound by no io_point",
        "unbound_channel device 2 channel \"ch3\": is bound by no io_point",
    ] {
        assert!(stdout.contains(line), "{line} missing from:\n{stdout}");
    }
    // A writable internal point is the ordinary setpoint mechanism, not
    // operator-surface review — io_point 13 is not flagged.
    assert!(
        !stdout.contains("writable_field_point io_point 13"),
        "{stdout}"
    );
}

#[test]
fn lint_clean_fixture_reports_no_findings_and_exits_zero() {
    let output = run_lint(&fixture("lint/clean.json"), &[]);
    assert!(output.status.success(), "{}", stderr(&output));
    assert_eq!(stdout(&output).trim(), "no findings");
}

#[test]
fn lint_strict_exits_nonzero_on_findings() {
    let output = run_lint(&fixture("lint/findings.json"), &["--strict"]);
    assert!(!output.status.success());
    let message = stderr(&output);
    assert!(
        message.contains("lint finding(s) under --strict"),
        "{message}"
    );
    assert!(
        message.contains("point_without_signal io_point 11"),
        "{message}"
    );
    // --strict on a clean model still exits zero.
    let clean = run_lint(&fixture("lint/clean.json"), &["--strict"]);
    assert!(clean.status.success(), "{}", stderr(&clean));
}

#[test]
fn lint_reports_an_invalid_document_with_its_validation_errors() {
    let invalid = fixture("invalid").join("unknown_channel.json");
    for extra in [Vec::new(), vec!["--strict"]] {
        let output = run_lint(&invalid, &extra);
        assert!(!output.status.success(), "lint unexpectedly succeeded");
        let stderr = stderr(&output);
        assert!(
            stderr.contains("io point 10 binds unknown channel \"ch9\" on device 1"),
            "stderr lacks the validation errors:\n{stderr}"
        );
        assert!(!stderr.contains("point_without_signal"), "{stderr}");
    }
}

#[test]
fn lint_output_is_deterministic_across_runs() {
    let first = run_lint(&fixture("lint/findings.json"), &[]);
    let second = run_lint(&fixture("lint/findings.json"), &[]);
    assert_eq!(stdout(&first), stdout(&second));
}

#[test]
fn lint_without_a_file_prints_usage() {
    let output = Command::new(BIN)
        .arg("lint")
        .output()
        .expect("failed to run dcs-model");
    assert!(!output.status.success());
    assert!(stderr(&output).contains("usage: dcs-model"));
}
