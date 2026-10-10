//! Drift check for `dcs-plant-server --dynamics-schema`: the emitted
//! dynamics-document JSON Schema must parse as a schema, stay
//! byte-identical across runs — the release record pins its sha256 like
//! the other recorded schemas — and validate the reference plant's
//! checked-in dynamics document.

use dcs_sim::ProcessElement;
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

/// The binary under test, built by Cargo alongside the test harness.
const SERVER: &str = env!("CARGO_BIN_EXE_dcs-plant-server");

fn run_dynamics_schema() -> Output {
    Command::new(SERVER)
        .arg("--dynamics-schema")
        .output()
        .expect("failed to run dcs-plant-server")
}

fn workspace_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap()
}

#[test]
fn dynamics_schema_emits_a_parseable_schema() {
    let output = run_dynamics_schema();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let document: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    jsonschema::validator_for(&document).expect("the emitted document must be a usable schema");
}

#[test]
fn dynamics_schema_output_is_deterministic_across_runs() {
    let first = run_dynamics_schema();
    let second = run_dynamics_schema();
    assert_eq!(first.stdout, second.stdout);
    // The entry function agrees with the flag's canonical form.
    let canonical = serde_json::to_string_pretty(&ProcessElement::json_schema()).unwrap();
    assert_eq!(
        String::from_utf8(first.stdout).unwrap().trim_end(),
        canonical.trim_end()
    );
}

fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[test]
fn recorded_release_dynamics_schema_matches_the_emitted_output() {
    // Prior records describe their own contract, even while publication is
    // pending. Freeze their bytes instead of rewriting them on each contract
    // change. Only the new v0.11 candidate must match this build's emission.
    let output = run_dynamics_schema();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    for (path, recorded_sha256) in [
        (
            "docs/releases/v0.3.0/dynamics.schema.json",
            Some("98fb4a4298c5974b8ab0adf1374cd6d53b0c2cfbd2e24a31c874090235b46f02"),
        ),
        (
            "docs/releases/v0.4.0/dynamics.schema.json",
            Some("98fb4a4298c5974b8ab0adf1374cd6d53b0c2cfbd2e24a31c874090235b46f02"),
        ),
        (
            "docs/releases/v0.5.0/dynamics.schema.json",
            Some("98fb4a4298c5974b8ab0adf1374cd6d53b0c2cfbd2e24a31c874090235b46f02"),
        ),
        (
            "docs/releases/v0.6.0/dynamics.schema.json",
            Some("98fb4a4298c5974b8ab0adf1374cd6d53b0c2cfbd2e24a31c874090235b46f02"),
        ),
        (
            "docs/releases/v0.7.0/dynamics.schema.json",
            Some("98fb4a4298c5974b8ab0adf1374cd6d53b0c2cfbd2e24a31c874090235b46f02"),
        ),
        (
            "docs/releases/v0.8.0/dynamics.schema.json",
            Some("98fb4a4298c5974b8ab0adf1374cd6d53b0c2cfbd2e24a31c874090235b46f02"),
        ),
        (
            "docs/releases/v0.9.0/dynamics.schema.json",
            Some("98fb4a4298c5974b8ab0adf1374cd6d53b0c2cfbd2e24a31c874090235b46f02"),
        ),
        (
            "docs/releases/v0.10.0/dynamics.schema.json",
            Some("98fb4a4298c5974b8ab0adf1374cd6d53b0c2cfbd2e24a31c874090235b46f02"),
        ),
    ] {
        let recorded = std::fs::read(workspace_root().join(path)).unwrap_or_else(|error| {
            panic!("the release record's dynamics schema file {path} must exist: {error}")
        });
        if let Some(expected) = recorded_sha256 {
            assert_eq!(
                sha256_hex(&recorded),
                expected,
                "{path}'s sha256 drifted from the digest its record publishes — \
                 regenerate the record file and update record.md"
            );
        }
    }
    let candidate =
        std::fs::read(workspace_root().join("docs/releases/v0.11.0/dynamics.schema.json")).unwrap();
    assert_eq!(
        sha256_hex(&candidate),
        "730a3dd6d80bf69f744a22d64e5c356e2d342f9603bbd9226cca5d2b2da0313d",
        "candidate record digest drift"
    );
    assert!(
        candidate == output.stdout,
        "v0.11 candidate drifted from the emitted schema; regenerate and update record.md"
    );
}

#[test]
fn dynamics_schema_validates_the_reference_plant_document() {
    // The acceptance criterion: the consumer-owned dynamics document
    // the deployment manifest names validates against the emitted
    // artifact.
    let output = run_dynamics_schema();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let emitted: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    let validator = jsonschema::validator_for(&emitted).unwrap();
    let text =
        std::fs::read_to_string(workspace_root().join("reference-plant/model/dynamics.json"))
            .unwrap();
    let document: serde_json::Value = serde_json::from_str(&text).unwrap();
    assert!(
        validator.is_valid(&document),
        "the emitted schema rejected reference-plant/model/dynamics.json: {}",
        validator
            .iter_errors(&document)
            .map(|error| format!("{error} at {}", error.instance_path()))
            .collect::<Vec<_>>()
            .join("; ")
    );
}

#[test]
fn dynamics_schema_rejects_other_arguments() {
    // The mode reads no documents — a model path or the document flags
    // are refused rather than silently ignored.
    for extra in [
        vec!["model.json"],
        vec!["--dynamics", "dynamics.json"],
        vec!["--listen", "127.0.0.1:0"],
    ] {
        let output = Command::new(SERVER)
            .arg("--dynamics-schema")
            .args(&extra)
            .output()
            .expect("failed to run dcs-plant-server");
        assert!(
            !output.status.success(),
            "--dynamics-schema {extra:?} unexpectedly succeeded"
        );
        assert!(
            String::from_utf8_lossy(&output.stderr).contains("--dynamics-schema"),
            "--dynamics-schema {extra:?}: stderr does not name the mode: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
}
