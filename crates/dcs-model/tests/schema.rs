//! Drift check for the emitted JSON Schema: `dcs-model schema` output must
//! parse as a schema, stay byte-identical across runs, and hold its verdicts
//! against every checked-in fixture document — the corpus guard that keeps
//! the hand-maintained schema honest against the serde types and the Rust
//! validator.
//!
//! The sweep walks every `*.json` under `crates/*/fixtures/`:
//!
//! - a document that does not deserialize as a [`PlantModel`] is not a model
//!   document at all (dynamics documents, the signal-index output) and the
//!   schema must reject it;
//! - a model document outside an `invalid/` directory must be accepted by
//!   both the schema and [`PlantModel::load`];
//! - a model document under an `invalid/` directory must appear in
//!   [`INVALID`], which pins its schema and load verdicts separately —
//!   several checked-in invalid fixtures exercise cross-reference, wiring,
//!   or assembly-layer rules the schema language cannot express, so the
//!   schema legitimately accepts documents `load` or assembly rejects
//!   (the recorded split, see `src/schema.rs` and `docs/architecture.md`).

use dcs_model::PlantModel;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::process::Command;

const BIN: &str = env!("CARGO_BIN_EXE_dcs-model");

/// `(schema accepts, load accepts)` per workspace-relative fixture path —
/// the pinned verdicts for model documents under `invalid/` directories.
/// Every such fixture must be listed: a new invalid fixture without an
/// entry fails the sweep, which is the point of the drift check.
fn invalid_expectations() -> std::collections::BTreeMap<&'static str, (bool, bool)> {
    [
        // dcs-model's invalid corpus: every entry fails `load`; the schema
        // rejects only what it can express.
        (
            "crates/dcs-model/fixtures/invalid/dangling_reference.json",
            (true, false),
        ),
        (
            "crates/dcs-model/fixtures/invalid/dangling_signal.json",
            (true, false),
        ),
        (
            "crates/dcs-model/fixtures/invalid/direction_mismatch.json",
            (true, false),
        ),
        (
            "crates/dcs-model/fixtures/invalid/duplicate_id.json",
            (false, false),
        ),
        (
            "crates/dcs-model/fixtures/invalid/internal_missing_initial.json",
            (false, false),
        ),
        (
            "crates/dcs-model/fixtures/invalid/unknown_channel.json",
            (true, false),
        ),
        // dcs-assembly's invalid corpus fails at assembly, not at `load`.
        // The schema still rejects the two whose known device-kind
        // parameter shape is broken: a `sim-tcp` without `address` and a
        // `sim-bus` register index past u16. The decision-70 alarm
        // fixtures fail construction — the missing record and the missing
        // parameters are also schema-visible through the kind-conditional
        // `then`; a whitespace-only prose field is not, so it keeps its
        // (accepted, accepted) verdicts for the construction seam to
        // reject.
        (
            "crates/dcs-assembly/fixtures/invalid/alarm_empty_rationalization.json",
            (true, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/alarm_missing_priority.json",
            (false, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/alarm_missing_rationalization.json",
            (false, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/alarm_missing_response_ticks.json",
            (false, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/bad_device_parameters.json",
            (false, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/bad_parameters.json",
            (true, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/bad_script.json",
            (true, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/bad_sim_bus_parameters.json",
            (false, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/bad_sim_cyclic_parameters.json",
            (false, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/direction_mismatch.json",
            (true, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/duplicate_channel.json",
            (true, true),
        ),
        // The `ethercat` corpus: the kind-conditional `then` rejects the
        // shape violations it can see — a missing `hardware` marker, a
        // missing `bus`, a mistyped identity field, a non-`fail` startup
        // policy — while the channel-table-dependent rules (offset
        // collisions, wrong-image placement, unmapped channels,
        // safe-output coverage) and the marker-on-sim honesty check stay
        // schema-accepted for the factory to reject at assembly.
        (
            "crates/dcs-assembly/fixtures/invalid/ethercat_bad_identity.json",
            (false, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/ethercat_bad_startup.json",
            (false, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/ethercat_missing_bus.json",
            (false, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/ethercat_missing_hardware.json",
            (false, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/ethercat_missing_safe_output.json",
            (true, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/ethercat_offset_collision.json",
            (true, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/ethercat_unmapped_channel.json",
            (true, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/ethercat_wrong_image.json",
            (true, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/hardware_marked_sim.json",
            (true, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/port_bound_twice.json",
            (true, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/type_mismatch.json",
            (true, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/unbound_port.json",
            (true, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/unknown_component_kind.json",
            (true, true),
        ),
        (
            "crates/dcs-assembly/fixtures/invalid/unknown_device_kind.json",
            (true, true),
        ),
    ]
    .into_iter()
    .collect()
}

fn workspace_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap()
}

/// Collects every `*.json` under `dir`, recursively.
fn json_files(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            json_files(&path, out);
        } else if path.extension().is_some_and(|ext| ext == "json") {
            out.push(path);
        }
    }
}

fn run_schema_subcommand() -> std::process::Output {
    Command::new(BIN)
        .arg("schema")
        .output()
        .expect("failed to run dcs-model")
}

#[test]
fn schema_subcommand_emits_a_parseable_schema() {
    let output = run_schema_subcommand();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let document: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    jsonschema::validator_for(&document).expect("the emitted document must be a usable schema");
}

#[test]
fn schema_output_is_deterministic_across_runs() {
    let first = run_schema_subcommand();
    let second = run_schema_subcommand();
    assert_eq!(first.stdout, second.stdout);
    // The entry function agrees with the subcommand's canonical form.
    let canonical = serde_json::to_string_pretty(&PlantModel::json_schema()).unwrap();
    assert_eq!(
        String::from_utf8(first.stdout).unwrap().trim_end(),
        canonical.trim_end()
    );
}

fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[test]
fn recorded_release_schema_matches_the_emitted_output() {
    // Each release record (docs/release-contract.md's release
    // procedure) carries `dcs-model schema`'s output at its recorded
    // commit; a checked-in file must stay byte-identical to what the
    // subcommand emits now or the published schema silently diverges
    // from the code before the tag is cut. Regenerate a record file
    // with `dcs-model schema > docs/releases/<tag>/plant-model.schema.json`
    // whenever the emitted schema legitimately changes — and update
    // the record's published sha256 with it while the tag is pending.
    //
    // `Some(digest)` asserts the file's sha256 equals the digest its
    // record publishes, keeping the checked-in artifact and the record
    // from drifting apart. `v0.1.0` and `v0.2.0` carry `None`: their
    // recorded sha256 pins the tagged emission, which the tracked file
    // has legitimately moved past since the cut — `v0.2.0`'s with the
    // `requires_reason` io_point field (#908).
    let output = run_schema_subcommand();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    for (path, recorded_sha256) in [
        (
            "docs/releases/v0.1.0/plant-model.schema.json",
            None::<&'static str>,
        ),
        ("docs/releases/v0.2.0/plant-model.schema.json", None),
    ] {
        let recorded = std::fs::read(workspace_root().join(path)).unwrap_or_else(|error| {
            panic!("the release record's schema file {path} must exist: {error}")
        });
        assert_eq!(
            recorded, output.stdout,
            "{path} drifted from `dcs-model schema`'s emitted output — \
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
fn schema_rejects_documents_with_structural_violations() {
    let schema = PlantModel::json_schema();
    let validator = jsonschema::validator_for(&schema).unwrap();
    let cases: &[serde_json::Value] = &[
        // Missing required sections.
        serde_json::json!({"version": 1}),
        // Wrong field type.
        serde_json::json!({"version": "one", "devices": [], "io_points": [],
            "signals": [], "components": [], "connections": []}),
        // Unsupported version.
        serde_json::json!({"version": 2, "devices": [], "io_points": [],
            "signals": [], "components": [], "connections": []}),
        // Unknown top-level key.
        serde_json::json!({"version": 1, "devices": [], "io_points": [],
            "signals": [], "components": [], "connections": [], "extra": 1}),
        // Unknown element key — serde would silently ignore it.
        serde_json::json!({"version": 1, "devices": [], "io_points": [
            {"id": 1, "direction": "in", "value_type": "float",
             "initial": {"float": 0.0}, "initials": {"float": 1.0}}
        ], "signals": [], "components": [], "connections": []}),
        // A writable out point.
        serde_json::json!({"version": 1, "devices": [], "io_points": [
            {"id": 1, "direction": "out", "value_type": "float",
             "initial": {"float": 0.0}, "writable": true}
        ], "signals": [], "components": [], "connections": []}),
        // An internal point whose initial kind mismatches value_type.
        serde_json::json!({"version": 1, "devices": [], "io_points": [
            {"id": 1, "direction": "in", "value_type": "float",
             "initial": {"bool": true}}
        ], "signals": [], "components": [], "connections": []}),
        // A bound point declaring an initial value.
        serde_json::json!({"version": 1, "devices": [
            {"id": 1, "kind": "sim", "channels":
                {"ch0": {"direction": "in", "value_type": "float"}}}
        ], "io_points": [
            {"id": 1, "direction": "in", "value_type": "float",
             "channel": {"device": 1, "name": "ch0"}, "initial": {"float": 0.0}}
        ], "signals": [], "components": [], "connections": []}),
        // A sim-tcp device without the required address parameter.
        serde_json::json!({"version": 1, "devices": [
            {"id": 1, "kind": "sim-tcp", "parameters": {"timeout_ms": 250},
             "channels": {"ch0": {"direction": "in", "value_type": "float"}}}
        ], "io_points": [], "signals": [], "components": [], "connections": []}),
        // A freshness budget on an out point.
        serde_json::json!({"version": 1, "devices": [
            {"id": 1, "kind": "sim", "channels":
                {"ch0": {"direction": "out", "value_type": "float"}}}
        ], "io_points": [
            {"id": 1, "direction": "out", "value_type": "float",
             "channel": {"device": 1, "name": "ch0"}, "stale_after_ticks": 2}
        ], "signals": [], "components": [], "connections": []}),
        // A freshness budget on a channel-less internal point.
        serde_json::json!({"version": 1, "devices": [], "io_points": [
            {"id": 1, "direction": "in", "value_type": "float",
             "initial": {"float": 0.0}, "stale_after_ticks": 2}
        ], "signals": [], "components": [], "connections": []}),
        // The journaled flag on a float point — the durable journal is
        // the discrete-transition record, not a per-scan stream.
        serde_json::json!({"version": 1, "devices": [], "io_points": [
            {"id": 1, "direction": "in", "value_type": "float",
             "initial": {"float": 0.0}, "journaled": true}
        ], "signals": [], "components": [], "connections": []}),
        // The requires-reason mark on an out point — the command
        // surface never reaches it, so the flag is a dead declaration.
        serde_json::json!({"version": 1, "devices": [], "io_points": [
            {"id": 1, "direction": "out", "value_type": "bool",
             "initial": {"bool": false}, "writable": false,
             "requires_reason": true}
        ], "signals": [], "components": [], "connections": []}),
        // And on an in point the model never marked writable — the
        // gate it qualifies admits nothing.
        serde_json::json!({"version": 1, "devices": [], "io_points": [
            {"id": 1, "direction": "in", "value_type": "bool",
             "initial": {"bool": false}, "requires_reason": true}
        ], "signals": [], "components": [], "connections": []}),
        // A negative freshness budget.
        serde_json::json!({"version": 1, "devices": [
            {"id": 1, "kind": "sim", "channels":
                {"ch0": {"direction": "in", "value_type": "float"}}}
        ], "io_points": [
            {"id": 1, "direction": "in", "value_type": "float",
             "channel": {"device": 1, "name": "ch0"}, "stale_after_ticks": -1}
        ], "signals": [], "components": [], "connections": []}),
    ];
    for (index, document) in cases.iter().enumerate() {
        assert!(
            !validator.is_valid(document),
            "case {index} unexpectedly valid: {document}"
        );
    }
}

#[test]
fn schema_pins_the_alarm_rationalization_obligation() {
    // Decision 70's kind-conditional rule: the known alarm kinds must
    // carry the `rationalization` block and the `priority`/`class`/
    // `response_ticks` parameters; other kinds may omit the block.
    let schema = PlantModel::json_schema();
    let validator = jsonschema::validator_for(&schema).unwrap();
    let model = |component: serde_json::Value| {
        serde_json::json!({
            "version": 1, "devices": [], "io_points": [], "signals": [],
            "components": [component], "connections": []
        })
    };
    let alarm_parameters = serde_json::json!({
        "priority": {"int": 1},
        "class": {"int": 2},
        "response_ticks": {"int": 30}
    });
    let record = serde_json::json!({
        "consequence": "The wet well overflows",
        "required_action": "Start the standby pump",
        "reference": "station-high-level"
    });

    // A fully rationalized alarm validates, and a non-alarm kind may
    // omit the block entirely.
    assert!(validator.is_valid(&model(serde_json::json!({
        "id": 1, "kind": "latching-alarm", "parameters": alarm_parameters,
        "rationalization": record, "ports": {}
    }))));
    assert!(validator.is_valid(&model(serde_json::json!({
        "id": 1, "kind": "motor", "parameters": {}, "ports": {}
    }))));

    for kind in [
        "latching-alarm",
        "bool-latching-alarm",
        "managed-latching-alarm",
        "managed-bool-latching-alarm",
    ] {
        // The record is obligatory...
        assert!(
            !validator.is_valid(&model(serde_json::json!({
                "id": 1, "kind": kind, "parameters": alarm_parameters, "ports": {}
            }))),
            "{kind}: missing rationalization unexpectedly valid"
        );
        // ... as is the parameter basis.
        assert!(
            !validator.is_valid(&model(serde_json::json!({
                "id": 1, "kind": kind, "parameters": {
                    "priority": {"int": 1}, "response_ticks": {"int": 30}
                },
                "rationalization": record, "ports": {}
            }))),
            "{kind}: missing `class` unexpectedly valid"
        );
        // A negative code fails the non-negative bound.
        assert!(
            !validator.is_valid(&model(serde_json::json!({
                "id": 1, "kind": kind,
                "parameters": {
                    "priority": {"int": -1}, "class": {"int": 2},
                    "response_ticks": {"int": 30}
                },
                "rationalization": record, "ports": {}
            }))),
            "{kind}: negative priority unexpectedly valid"
        );
        // An empty prose field fails the record's own rule.
        assert!(
            !validator.is_valid(&model(serde_json::json!({
                "id": 1, "kind": kind, "parameters": alarm_parameters,
                "rationalization": {
                    "consequence": "", "required_action": "Respond",
                    "reference": "ref"
                },
                "ports": {}
            }))),
            "{kind}: empty consequence unexpectedly valid"
        );
    }
}

#[test]
fn schema_pins_the_ethercat_declaration() {
    // The `ethercat` kind-conditional: a hardware-bound device declares
    // `hardware: true` plus the `bus`/`identity`/`mapping`/
    // `exchange_miss_threshold`/`startup` parameter shapes. The schema
    // pins the shapes; the channel-table-dependent halves stay with the
    // factory.
    let schema = PlantModel::json_schema();
    let validator = jsonschema::validator_for(&schema).unwrap();
    let model = |device: serde_json::Value| {
        serde_json::json!({
            "version": 1, "devices": [device], "io_points": [], "signals": [],
            "components": [], "connections": []
        })
    };
    let declaration = serde_json::json!({
        "bus": "ecat0",
        "identity": {"vendor": 21, "product": 750354, "revision": 1},
        "mapping": {
            "inputs": {"di0": {"byte": 0, "bit": 0}},
            "outputs": {"do0": {"byte": 0, "bit": 0}}
        },
        "exchange_miss_threshold": 3,
        "safe_outputs": {"do0": {"bool": false}},
        "startup": {"on_mismatch": "fail"}
    });
    let channels = serde_json::json!({
        "di0": {"direction": "in", "value_type": "bool"},
        "do0": {"direction": "out", "value_type": "bool"}
    });

    // A well-formed declaration validates…
    assert!(validator.is_valid(&model(serde_json::json!({
        "id": 1, "kind": "ethercat", "channels": channels,
        "hardware": true, "parameters": declaration
    }))));

    // …the hardware marker is required…
    assert!(!validator.is_valid(&model(serde_json::json!({
        "id": 1, "kind": "ethercat", "channels": channels,
        "parameters": declaration
    }))));
    assert!(!validator.is_valid(&model(serde_json::json!({
        "id": 1, "kind": "ethercat", "channels": channels,
        "hardware": false, "parameters": declaration
    }))));

    let mutated = |edit: &dyn Fn(&mut serde_json::Map<String, serde_json::Value>)| {
        let mut parameters = declaration.as_object().unwrap().clone();
        edit(&mut parameters);
        model(serde_json::json!({
            "id": 1, "kind": "ethercat", "channels": channels,
            "hardware": true, "parameters": parameters
        }))
    };

    // …a missing bus…
    assert!(!validator.is_valid(&mutated(&|p| {
        p.remove("bus");
    })));
    // …a mistyped identity field…
    assert!(!validator.is_valid(&mutated(&|p| {
        p["identity"]["vendor"] = serde_json::json!("wago");
    })));
    // …a zero miss threshold…
    assert!(!validator.is_valid(&mutated(&|p| {
        p["exchange_miss_threshold"] = serde_json::json!(0);
    })));
    // …a startup policy other than fail…
    assert!(!validator.is_valid(&mutated(&|p| {
        p["startup"]["on_mismatch"] = serde_json::json!("simulate");
    })));
    // …a host interface — there is no such parameter; an unknown key…
    assert!(!validator.is_valid(&mutated(&|p| {
        p.insert("interface".to_string(), serde_json::json!("eth0"));
    })));
    // …and a bit index past a byte.
    assert!(!validator.is_valid(&mutated(&|p| {
        p["mapping"]["inputs"]["di0"] = serde_json::json!({"byte": 0, "bit": 8});
    })));
}

#[test]
fn fixture_corpus_matches_the_schema_verdicts() {
    let schema = PlantModel::json_schema();
    let validator = jsonschema::validator_for(&schema).unwrap();
    let expectations = invalid_expectations();

    let root = workspace_root();
    let mut files = Vec::new();
    let crates = root.join("crates");
    for entry in std::fs::read_dir(&crates).unwrap().flatten() {
        json_files(&entry.path().join("fixtures"), &mut files);
    }
    files.sort();
    assert!(files.len() > 10, "fixture sweep found too few files");

    let mut seen_invalid = BTreeSet::new();
    for path in &files {
        let relative = path
            .strip_prefix(&root)
            .unwrap()
            .to_str()
            .unwrap()
            .to_owned();
        let Ok(text) = std::fs::read_to_string(path) else {
            continue;
        };
        let Ok(document) = serde_json::from_str::<serde_json::Value>(&text) else {
            // Not JSON at all (e.g. a deliberately malformed dynamics
            // document) — outside the corpus.
            continue;
        };
        let Ok(model) = serde_json::from_value::<PlantModel>(document.clone()) else {
            // A checked-in document of another type must not validate as a
            // plant model.
            assert!(
                !validator.is_valid(&document),
                "{relative}: a non-model document passed the schema"
            );
            continue;
        };
        let schema_ok = validator.is_valid(&document);
        let load_ok = PlantModel::load(&text).is_ok();
        if relative.contains("/invalid/") {
            let Some(&(want_schema, want_load)) = expectations.get(relative.as_str()) else {
                panic!("{relative}: invalid fixture missing from the verdict manifest");
            };
            seen_invalid.insert(relative.clone());
            assert_eq!(
                (schema_ok, load_ok),
                (want_schema, want_load),
                "{relative}: schema/load verdicts diverged"
            );
        } else {
            assert!(
                schema_ok,
                "{relative}: valid fixture rejected by the schema: {}",
                validator
                    .iter_errors(&document)
                    .map(|error| error.to_string())
                    .collect::<Vec<_>>()
                    .join("; ")
            );
            assert!(load_ok, "{relative}: valid fixture failed PlantModel::load");
            // The serde re-serialization of the parsed model must validate
            // too — the schema has to cover the shape serde emits, not only
            // the shape the fixtures happen to be written in.
            let emitted = serde_json::to_value(&model).unwrap();
            assert!(
                validator.is_valid(&emitted),
                "{relative}: re-serialized model rejected by the schema"
            );
        }
    }

    // The manifest must name exactly the invalid fixtures present — no
    // stale entries, no unswept files. Non-model documents need no
    // manifest: each is schema-rejected individually above, and a foreign
    // document set grows without a schema decision to record.
    let expected: BTreeSet<String> = expectations.keys().map(|k| k.to_string()).collect();
    assert_eq!(seen_invalid, expected, "invalid-fixture manifest drifted");
}
