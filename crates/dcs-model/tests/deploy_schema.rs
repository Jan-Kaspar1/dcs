//! Drift check for `dcs-model deploy-schema`: the emitted
//! deployment-manifest JSON Schema must parse as a schema, stay
//! byte-identical across runs — the release record pins its sha256 like
//! the other recorded schemas — and hold its verdicts against the
//! reference plant's checked-in `deploy/manifest.json` plus the
//! documented optional-field variants the deploy stage exercises
//! (persistence-omitted, topology-declared, topology-multi-pair), while
//! rejecting malformed documents with a diagnostic naming the offending
//! field.

use dcs_model::deployment_manifest_schema;
use std::path::{Path, PathBuf};
use std::process::Command;

const BIN: &str = env!("CARGO_BIN_EXE_dcs-model");

fn run_deploy_schema_subcommand() -> std::process::Output {
    Command::new(BIN)
        .arg("deploy-schema")
        .output()
        .expect("failed to run dcs-model")
}

fn workspace_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap()
}

fn reference_manifest() -> serde_json::Value {
    let text =
        std::fs::read_to_string(workspace_root().join("reference-plant/deploy/manifest.json"))
            .unwrap();
    serde_json::from_str(&text).unwrap()
}

fn validator() -> jsonschema::Validator {
    jsonschema::validator_for(&deployment_manifest_schema())
        .expect("the emitted document must be a usable schema")
}

#[test]
fn deploy_schema_subcommand_emits_a_parseable_schema() {
    let output = run_deploy_schema_subcommand();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let document: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    jsonschema::validator_for(&document).expect("the emitted document must be a usable schema");
}

#[test]
fn deploy_schema_output_is_deterministic_across_runs() {
    let first = run_deploy_schema_subcommand();
    let second = run_deploy_schema_subcommand();
    assert_eq!(first.stdout, second.stdout);
    // The entry function agrees with the subcommand's canonical form.
    let canonical = serde_json::to_string_pretty(&deployment_manifest_schema()).unwrap();
    assert_eq!(
        String::from_utf8(first.stdout).unwrap().trim_end(),
        canonical.trim_end()
    );
}

#[test]
fn deploy_schema_subcommand_rejects_arguments() {
    // The mode reads no documents — a stray argument is refused rather
    // than silently ignored.
    let output = Command::new(BIN)
        .args(["deploy-schema", "manifest.json"])
        .output()
        .expect("failed to run dcs-model");
    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("deploy-schema"),
        "stderr does not name the subcommand: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn deploy_schema_validates_the_reference_manifest() {
    // The acceptance criterion: the consumer-owned manifest
    // `reference-plant/deploy/compose.yaml` instantiates validates
    // against the emitted artifact.
    let document = reference_manifest();
    let validator = validator();
    assert!(
        validator.is_valid(&document),
        "the emitted schema rejected reference-plant/deploy/manifest.json: {}",
        validator
            .iter_errors(&document)
            .map(|error| format!("{error} at {}", error.instance_path()))
            .collect::<Vec<_>>()
            .join("; ")
    );
}

#[test]
fn deploy_schema_validates_the_documented_optional_variants() {
    // The optional-field shapes the reference plant's deploy stage
    // exercises (`ci/check.sh`'s rig cases): the persistence-omitted
    // deployment, the topology-declared single pair, and the
    // topology-multi-pair four-controller rig. The last stays
    // schema-valid under decision 99 — the one-field bound is a
    // count over absent `standby` keys the vocabulary cannot
    // express, so the rig check's `rig-mismatch` is its rejection.
    let validator = validator();

    // persistence-omitted: both per-controller durability fields dropped.
    let mut omitted = reference_manifest();
    for controller in omitted["controllers"].as_array_mut().unwrap() {
        let entry = controller.as_object_mut().unwrap();
        entry.remove("state_file");
        entry.remove("journal_file");
    }

    // topology-declared: the one named pair the single-pair deployment
    // may carry.
    let mut declared = reference_manifest();
    declared["topology"] = serde_json::json!({
        "pairs": [{"name": "station", "members": ["ctrl-a", "ctrl-b"]}]
    });

    // topology-multi-pair: two named pairs over a four-controller rig.
    let mut multi = declared.clone();
    multi["controllers"].as_array_mut().unwrap().extend([
        serde_json::json!({
            "name": "ctrl-c",
            "listen": "0.0.0.0:8082",
            "state_file": "/var/tmp/state.json",
            "journal_file": "/var/tmp/journal.jsonl"
        }),
        serde_json::json!({
            "name": "ctrl-d",
            "listen": "0.0.0.0:8083",
            "standby": "ctrl-c:8082",
            "state_file": "/var/tmp/state.json",
            "journal_file": "/var/tmp/journal.jsonl"
        }),
    ]);
    multi["topology"]["pairs"]
        .as_array_mut()
        .unwrap()
        .push(serde_json::json!({
            "name": "station-b", "members": ["ctrl-c", "ctrl-d"]
        }));
    multi["topology"]["pairs"][0]["name"] = serde_json::json!("station-a");

    for (name, document) in [
        ("persistence-omitted", omitted),
        ("topology-declared", declared),
        ("topology-multi-pair", multi),
    ] {
        assert!(
            validator.is_valid(&document),
            "the {name} variant unexpectedly failed: {}",
            validator
                .iter_errors(&document)
                .map(|error| format!("{error} at {}", error.instance_path()))
                .collect::<Vec<_>>()
                .join("; ")
        );
    }
}

#[test]
fn deploy_schema_rejects_malformed_documents_with_named_paths() {
    // Every malformed shape fails, and its diagnostics name the
    // offending field's path.
    let validator = validator();
    let manifest = reference_manifest();
    let mutated = |edit: &dyn Fn(&mut serde_json::Value)| {
        let mut document = manifest.clone();
        edit(&mut document);
        document
    };
    let cases: Vec<serde_json::Value> = vec![
        // A missing required section.
        mutated(&|d| {
            d.as_object_mut().unwrap().remove("images");
        }),
        // An unknown top-level key — a misspelled field must not
        // evaporate.
        mutated(&|d| {
            d.as_object_mut()
                .unwrap()
                .insert("toplogy".to_owned(), serde_json::json!({}));
        }),
        // A malformed fingerprint shape.
        mutated(&|d| {
            d["model"]["fingerprint"] = serde_json::json!("not-hex");
        }),
        // A `failover_budget` on a duty entry — the field qualifies a
        // tracking standby only.
        mutated(&|d| {
            d["controllers"][0]["failover_budget"] = serde_json::json!(2);
        }),
        // A non-positive failover budget.
        mutated(&|d| {
            d["controllers"][1]["failover_budget"] = serde_json::json!(0);
        }),
        // A standby address carrying no port.
        mutated(&|d| {
            d["controllers"][1]["standby"] = serde_json::json!("ctrl-a");
        }),
        // An empty controller set — the deployment declares nothing.
        mutated(&|d| {
            d["controllers"] = serde_json::json!([]);
        }),
        // A named pair declaring three members.
        mutated(&|d| {
            d["topology"] = serde_json::json!({
                "pairs": [{"name": "station",
                           "members": ["ctrl-a", "ctrl-b", "ctrl-c"]}]
            });
        }),
        // A named pair whose two members are the same controller.
        mutated(&|d| {
            d["topology"] = serde_json::json!({
                "pairs": [{"name": "station",
                           "members": ["ctrl-a", "ctrl-a"]}]
            });
        }),
        // An empty pairs list.
        mutated(&|d| {
            d["topology"] = serde_json::json!({"pairs": []});
        }),
    ];
    for (index, document) in cases.iter().enumerate() {
        assert!(
            !validator.is_valid(document),
            "case {index} unexpectedly valid: {document}"
        );
    }

    // A rejection names the offending field's path — the malformed
    // fingerprint reports `/model/fingerprint`, not an anonymous index.
    let document = mutated(&|d| {
        d["model"]["fingerprint"] = serde_json::json!("not-hex");
    });
    let diagnostics: Vec<String> = validator
        .iter_errors(&document)
        .map(|error| error.instance_path().to_string())
        .collect();
    assert!(
        diagnostics.iter().any(|path| path == "/model/fingerprint"),
        "no diagnostic names the malformed fingerprint: {diagnostics:?}"
    );
}

#[test]
fn deploy_schema_leaves_the_referential_rules_check_side() {
    // The membership rules the schema vocabulary cannot express stay
    // with the rig-definition check: a member naming no declared
    // controller and a pair whose standby wiring leaves it are
    // schema-valid shapes — the deploy stage's `rig-mismatch` is their
    // rejection.
    let validator = validator();
    let mut undeclared = reference_manifest();
    undeclared["topology"] = serde_json::json!({
        "pairs": [{"name": "station", "members": ["ctrl-a", "ctrl-z"]}]
    });
    assert!(
        validator.is_valid(&undeclared),
        "an undeclared member is check-side, not schema-rejected"
    );

    // A pair naming the same member across two pairs — disjoint
    // membership needs the cross-reference the vocabulary lacks.
    let mut shared = reference_manifest();
    shared["topology"] = serde_json::json!({
        "pairs": [
            {"name": "station-a", "members": ["ctrl-a", "ctrl-b"]},
            {"name": "station-b", "members": ["ctrl-a", "ctrl-d"]}
        ]
    });
    assert!(
        validator.is_valid(&shared),
        "a shared member is check-side, not schema-rejected"
    );

    // A second duty controller — an entry without `standby` — over the
    // manifest's one plant: decision 99's one-field bound counts absent
    // keys, which the schema vocabulary cannot express, so the
    // undeployable shape stays schema-valid and the rig check's
    // `rig-mismatch` is its rejection.
    let mut second_duty = reference_manifest();
    second_duty["controllers"]
        .as_array_mut()
        .unwrap()
        .push(serde_json::json!({
            "name": "ctrl-c",
            "listen": "0.0.0.0:8082"
        }));
    assert!(
        validator.is_valid(&second_duty),
        "a second duty entry is check-side, not schema-rejected"
    );
}
