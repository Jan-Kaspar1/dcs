//! Drift check for the served block-interface registry's emitted JSON
//! Schema: `dcs-model interface-schema` output must parse as a schema,
//! stay byte-identical across runs, and hold its verdicts against a
//! served-registry document — the same guard `tests/schema.rs` keeps on
//! the plant-model schema, for the decision-82 served contract.
//!
//! The all-kind sweep — every registered kind's derived
//! [`BlockInterface`](dcs_core::BlockInterface) validated against the
//! emitted schema — lives in `dcs-blocks`' `spec_drift` test, where the
//! registered `KIND` set is already pinned; here the emitted artifact
//! itself is pinned: parseability, determinism, and the verdicts on
//! checked-in registry documents.

use dcs_core::{
    CommandAvailability, CommandDecl, ComponentDescriptor, EventDecl, EventField, EventFieldKind,
    EventRetention, PointId, PortDescriptor, PortRole, SchemaView, Tick,
};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::process::Command;

const BIN: &str = env!("CARGO_BIN_EXE_dcs-model");

fn fixture(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("fixtures")
        .join(name)
}

fn run_interface_schema_subcommand() -> std::process::Output {
    Command::new(BIN)
        .arg("interface-schema")
        .output()
        .expect("failed to run dcs-model")
}

fn workspace_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap()
}

fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[test]
fn interface_schema_subcommand_emits_a_parseable_schema() {
    let output = run_interface_schema_subcommand();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let document: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    jsonschema::validator_for(&document).expect("the emitted document must be a usable schema");
}

#[test]
fn interface_schema_output_is_deterministic_across_runs() {
    let first = run_interface_schema_subcommand();
    let second = run_interface_schema_subcommand();
    assert_eq!(first.stdout, second.stdout);
    // The entry function agrees with the subcommand's canonical form.
    let canonical = serde_json::to_string_pretty(&SchemaView::json_schema()).unwrap();
    assert_eq!(
        String::from_utf8(first.stdout).unwrap().trim_end(),
        canonical.trim_end()
    );
}

#[test]
fn recorded_release_interface_schema_matches_the_emitted_output() {
    // `v0.2.0` is the first release record carrying `dcs-model
    // interface-schema`'s output — `v0.1.0`'s recorded commit predates
    // the served registry (#375). The same convention as the
    // plant-model schema pin in `tests/schema.rs`: the checked-in file
    // must stay byte-identical to the emitted output, and `Some`
    // asserts its sha256 equals the digest its record publishes.
    // Regenerate with `dcs-model interface-schema >
    // docs/releases/<tag>/block-interfaces.schema.json` whenever the
    // emitted schema legitimately changes — and update the record's
    // published sha256 with it while the tag is pending. `v0.2.0`'s
    // tag is cut, so its recorded sha256 pins the tagged emission and
    // the tracked file may legitimately move past it (`None`);
    // `v0.3.0`'s tag is pending, so its artifact and published sha256
    // are still pinned.
    let output = run_interface_schema_subcommand();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    for (path, recorded_sha256) in [
        (
            "docs/releases/v0.2.0/block-interfaces.schema.json",
            None::<&'static str>,
        ),
        (
            "docs/releases/v0.3.0/block-interfaces.schema.json",
            Some("ddc00496814a4e8cd0d6ec8a5d9fbb95e83f518dcd927b17a4802f13ac84013a"),
        ),
    ] {
        let recorded = std::fs::read(workspace_root().join(path)).unwrap_or_else(|error| {
            panic!("the release record's interface-schema file {path} must exist: {error}")
        });
        assert_eq!(
            recorded, output.stdout,
            "{path} drifted from `dcs-model interface-schema`'s emitted output — \
             regenerate the record file"
        );
        if let Some(expected) = recorded_sha256 {
            assert_eq!(
                sha256_hex(&recorded),
                expected,
                "{path}'s sha256 drifted from the digest its record publishes — \
                 regenerate the record file and update record.md"
            );
        }
    }
}

#[test]
fn interface_schema_accepts_a_derived_registry_document() {
    // The served form: `block_interfaces` over a bound-point-annotated
    // descriptor — the derivation `GET /schema` runs — wrapped in the
    // `SchemaView` envelope the endpoint stamps. A kind declaring
    // native commands and events exercises the `Declared` provenance
    // and the declared-retention vocabulary too.
    let descriptor = ComponentDescriptor {
        name: "seq".to_string(),
        kind: "sequencer".to_string(),
        label: "seq".to_string(),
        ports: vec![
            PortDescriptor {
                name: "advance".to_string(),
                direction: dcs_core::Direction::In,
                kind: dcs_core::ValueKind::Bool,
                role: Some(PortRole::Setpoint),
                point: Some(PointId(10)),
            },
            PortDescriptor {
                name: "running".to_string(),
                direction: dcs_core::Direction::Out,
                kind: dcs_core::ValueKind::Bool,
                role: Some(PortRole::Status),
                point: Some(PointId(20)),
            },
        ],
        parameters: Vec::new(),
        commands: vec![CommandDecl {
            name: "reset".to_string(),
            request: Vec::new(),
            availability: CommandAvailability::KindDeclared,
        }],
        events: vec![EventDecl {
            name: "step_completed".to_string(),
            payload: vec![EventField {
                name: "step".to_string(),
                kind: EventFieldKind::Value(dcs_core::ValueKind::Int),
                optional: false,
            }],
            retention: EventRetention::History,
        }],
    };
    let interface = dcs_core::block_interfaces(std::slice::from_ref(&descriptor))
        .into_iter()
        .next()
        .unwrap();
    let document = serde_json::to_value(SchemaView {
        publication: 3,
        tick: Tick(12),
        interfaces: vec![dcs_core::ComponentInterface {
            name: descriptor.name.clone(),
            interface,
        }],
    })
    .unwrap();

    let validator = jsonschema::validator_for(&SchemaView::json_schema()).unwrap();
    assert!(
        validator.is_valid(&document),
        "a derived served-registry document must validate: {}",
        validator
            .iter_errors(&document)
            .map(|error| error.to_string())
            .collect::<Vec<_>>()
            .join("; ")
    );
}

#[test]
fn interface_schema_accepts_the_full_declared_vocabulary() {
    // The hand-written counterpart covering what the derivation emits
    // rarely or never: an `engineering_fixed` capability, `latest`
    // retention, `unit` annotation, and every `adapted`/`emission`/
    // `availability` spelling.
    let document = serde_json::json!({
        "publication": 0,
        "tick": 0,
        "interfaces": [{
            "name": "lic-101",
            "interface": {
                "version": 1,
                "kind": "pid",
                "measurements": [
                    {"name": "pv", "direction": "in", "kind": "float",
                     "role": "process_value", "point": 10, "unit": "degC"},
                    {"name": "out", "direction": "out", "kind": "float",
                     "role": "output", "point": 20}
                ],
                "configuration": [
                    {"name": "kp", "kind": "float", "capability": "tunable",
                     "range": {"min": {"float": 0.0}, "max": {"float": 10.0}}},
                    {"name": "mode", "kind": "int", "capability": "engineering_fixed"}
                ],
                "state": [
                    {"name": "saturated", "direction": "out", "kind": "bool",
                     "role": "status", "point": 21, "persistence": "bound_point"}
                ],
                "commands": [
                    {"name": "write_value:sp", "request": [{"name": "value", "kind": "float"}],
                     "availability": "bound_point_writable", "adapted": "write_value",
                     "point": 11},
                    {"name": "force_point:sp", "request": [{"name": "value", "kind": "float"}],
                     "availability": "bound_point_writable", "adapted": "force_point",
                     "point": 11},
                    {"name": "unforce_point:sp", "request": [],
                     "availability": "bound_point_writable", "adapted": "unforce_point",
                     "point": 11},
                    {"name": "set_parameter:kp", "request": [{"name": "value", "kind": "float"}],
                     "availability": "always", "adapted": "set_parameter"},
                    {"name": "stroke_test", "request": [{"name": "ticks", "kind": "int"}],
                     "availability": "kind_declared", "adapted": "declared"}
                ],
                "events": [
                    {"name": "point_changed:sp",
                     "payload": [
                         {"name": "from", "kind": {"value": "float"}, "optional": true},
                         {"name": "to", "kind": {"value": "float"}}
                     ],
                     "retention": "journal", "emission": "when_journaled",
                     "adapted": "point_changed", "point": 11},
                    {"name": "quality_changed:pv",
                     "payload": [
                         {"name": "from", "kind": "quality", "optional": true},
                         {"name": "to", "kind": "quality"}
                     ],
                     "retention": "journal", "emission": "on_observed_change",
                     "adapted": "quality_changed", "point": 10},
                    {"name": "command_settled",
                     "payload": [{"name": "receipt", "kind": "receipt"}],
                     "retention": "journal", "emission": "on_command_settled",
                     "adapted": "command_settled"},
                    {"name": "step_failed",
                     "payload": [{"name": "error", "kind": "text"}],
                     "retention": "journal", "emission": "on_step_failure",
                     "adapted": "step_failed"},
                    {"name": "stroke_complete",
                     "payload": [{"name": "ticks", "kind": {"value": "int"}}],
                     "retention": "latest", "emission": "kind_emitted",
                     "adapted": "declared"}
                ]
            }
        }]
    });
    let validator = jsonschema::validator_for(&SchemaView::json_schema()).unwrap();
    assert!(
        validator.is_valid(&document),
        "the full declared vocabulary must validate: {}",
        validator
            .iter_errors(&document)
            .map(|error| error.to_string())
            .collect::<Vec<_>>()
            .join("; ")
    );
}

#[test]
fn interface_schema_rejects_the_malformed_fixture_with_named_diagnostics() {
    // `fixtures/interface-registry/malformed.json` violates the
    // declared collection shape (`commands` is an object, not an
    // array) and the retention vocabulary (`"archived"`): the
    // validator must refuse it, and its diagnostics must name the
    // violating elements.
    let text = std::fs::read_to_string(fixture("interface-registry/malformed.json")).unwrap();
    let document: serde_json::Value = serde_json::from_str(&text).unwrap();
    let validator = jsonschema::validator_for(&SchemaView::json_schema()).unwrap();
    assert!(
        !validator.is_valid(&document),
        "the malformed registry document unexpectedly validated"
    );
    let diagnostics: Vec<String> = validator
        .iter_errors(&document)
        .map(|error| format!("{} at {}", error, error.instance_path()))
        .collect();
    assert!(
        diagnostics
            .iter()
            .any(|error| error.contains("/interfaces/0/interface/commands")),
        "no diagnostic names the malformed commands collection: {diagnostics:?}"
    );
    assert!(
        diagnostics.iter().any(|error| error.contains("retention")),
        "no diagnostic names the out-of-vocabulary retention: {diagnostics:?}"
    );
}

#[test]
fn interface_schema_rejects_documents_with_structural_violations() {
    let validator = jsonschema::validator_for(&SchemaView::json_schema()).unwrap();
    let registry = |interface: serde_json::Value| {
        serde_json::json!({
            "publication": 0,
            "tick": 0,
            "interfaces": [{"name": "x", "interface": interface}]
        })
    };
    let minimal = serde_json::json!({
        "version": 1, "kind": "k", "measurements": [], "configuration": [],
        "state": [], "commands": [], "events": []
    });
    let cases: &[serde_json::Value] = &[
        // Missing the envelope fields.
        serde_json::json!({"interfaces": []}),
        // `interfaces` is not an array.
        serde_json::json!({"publication": 0, "tick": 0, "interfaces": {}}),
        // An interface missing a declared collection.
        registry(serde_json::json!({
            "version": 1, "kind": "k", "measurements": [],
            "configuration": [], "commands": [], "events": []
        })),
        // An interface version outside the contract.
        registry(serde_json::json!({
            "version": 2, "kind": "k", "measurements": [], "configuration": [],
            "state": [], "commands": [], "events": []
        })),
        // An unknown top-level key — serde would silently ignore it.
        serde_json::json!({"publication": 0, "tick": 0, "interfaces": [], "extra": 1}),
        // A misspelled field inside an interface entry.
        registry(serde_json::json!({
            "version": 1, "kind": "k", "measurements": [], "configuration": [],
            "state": [], "commands": [], "events": [], "event": []
        })),
        // A measurement missing its declared direction.
        registry({
            let mut interface = minimal.clone();
            interface["measurements"] = serde_json::json!([{"name": "pv", "kind": "float"}]);
            interface
        }),
        // An out-of-vocabulary availability.
        registry({
            let mut interface = minimal.clone();
            interface["commands"] = serde_json::json!([{
                "name": "write_value:sp", "request": [],
                "availability": "sometimes", "adapted": "write_value"
            }]);
            interface
        }),
        // An out-of-vocabulary event-field payload kind.
        registry({
            let mut interface = minimal.clone();
            interface["events"] = serde_json::json!([{
                "name": "command_settled",
                "payload": [{"name": "receipt", "kind": "json"}],
                "retention": "journal", "emission": "on_command_settled",
                "adapted": "command_settled"
            }]);
            interface
        }),
        // A legacy PascalCase spelling the serde reader still accepts
        // but the emitted vocabulary does not carry.
        registry({
            let mut interface = minimal.clone();
            interface["measurements"] = serde_json::json!([{
                "name": "pv", "direction": "in", "kind": "Float"
            }]);
            interface
        }),
    ];
    for (index, document) in cases.iter().enumerate() {
        assert!(
            !validator.is_valid(document),
            "case {index} unexpectedly valid: {document}"
        );
    }
}
