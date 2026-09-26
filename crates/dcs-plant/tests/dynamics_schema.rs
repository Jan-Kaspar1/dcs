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
    // `v0.3.0` is the first release record carrying `dcs-plant-server
    // --dynamics-schema`'s output — `v0.1.0` and `v0.2.0`'s recorded
    // commits predate the dynamics-document schema emission (#870).
    // The same convention as the plant-model schema pin in
    // `dcs-model`'s `tests/schema.rs`: the checked-in file must stay
    // byte-identical to the emitted output, and while the tag is
    // pending its sha256 must equal the digest
    // `docs/releases/v0.3.0/record.md` publishes. Regenerate with
    // `dcs-plant-server --dynamics-schema >
    // docs/releases/<tag>/dynamics.schema.json` whenever the emitted
    // schema legitimately changes — and update the record's published
    // sha256 with it while the tag is pending.
    let output = run_dynamics_schema();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let path = "docs/releases/v0.3.0/dynamics.schema.json";
    let recorded = std::fs::read(workspace_root().join(path)).unwrap_or_else(|error| {
        panic!("the release record's dynamics schema file {path} must exist: {error}")
    });
    assert_eq!(
        recorded, output.stdout,
        "{path} drifted from `dcs-plant-server --dynamics-schema`'s emitted output — \
         regenerate the record file"
    );
    assert_eq!(
        sha256_hex(&recorded),
        "98fb4a4298c5974b8ab0adf1374cd6d53b0c2cfbd2e24a31c874090235b46f02",
        "{path}'s sha256 drifted from the digest its record publishes — \
         regenerate the record file and update record.md"
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
