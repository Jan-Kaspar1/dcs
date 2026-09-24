//! Drift check for the dynamics document's emitted JSON Schema:
//! [`ProcessElement::json_schema`] must parse as a schema, stay
//! deterministic across calls, cover every element kind and field the
//! `--dynamics` merge accepts, and hold its verdicts against every
//! checked-in dynamics fixture — the same guard
//! `dcs-model/tests/schema.rs` keeps on the plant-model schema, for the
//! decision-24 document.
//!
//! The sweep walks every `*dynamics*.json` under `crates/*/fixtures/`
//! plus the consumer-side documents (`reference-plant/model/` and
//! `qa_lane/fixtures/`):
//!
//! - a checked-in dynamics document outside `invalid/` must deserialize
//!   as `Vec<ProcessElement>` *and* validate — the emitted schema is a
//!   first screen over exactly the grammar the merge reads, including
//!   the serde re-serialization of each document;
//! - a dynamics fixture under `invalid/` carries a pinned verdict —
//!   their offenses (unbound points, driven-point conflicts, an
//!   element driving an `out` point) are merge-layer rules the schema
//!   language cannot express, so the schema legitimately accepts what
//!   `ChannelMap::validate` rejects (the recorded split, see
//!   `src/schema.rs` and `docs/architecture.md`).

use dcs_core::PointId;
use dcs_sim::{
    BoolFlow, DeadTime, FirstOrderLag, FlowSum, Integrator, Noise, ProcessElement, ScaledFlow,
    SecondOrderLag, Threshold,
};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

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

/// Every checked-in dynamics document, workspace-relative: the
/// `*dynamics*.json` fixtures under `crates/` plus the consumer-side
/// documents outside the workspace crates.
fn dynamics_documents() -> Vec<PathBuf> {
    let root = workspace_root();
    let mut files = Vec::new();
    for entry in std::fs::read_dir(root.join("crates")).unwrap().flatten() {
        json_files(&entry.path().join("fixtures"), &mut files);
    }
    json_files(&root.join("qa_lane/fixtures"), &mut files);
    files.push(root.join("reference-plant/model/dynamics.json"));
    files.retain(|path| {
        path.file_name()
            .is_some_and(|name| name.to_string_lossy().contains("dynamics"))
    });
    files.sort();
    files
}

fn validator() -> jsonschema::Validator {
    jsonschema::validator_for(&ProcessElement::json_schema())
        .expect("the emitted document must be a usable schema")
}

#[test]
fn emitted_schema_is_a_usable_schema() {
    validator();
}

#[test]
fn emitted_schema_is_deterministic_across_calls() {
    // The release record pins the emitted bytes by sha256 like the
    // other recorded schemas, so the document must be identical on
    // every emission.
    assert_eq!(ProcessElement::json_schema(), ProcessElement::json_schema());
    assert_eq!(
        serde_json::to_string_pretty(&ProcessElement::json_schema()).unwrap(),
        serde_json::to_string_pretty(&ProcessElement::json_schema()).unwrap()
    );
}

/// One `ProcessElement` per declared variant — the coverage set the
/// schema must accept, serialized the way a document writes it.
fn one_element_per_kind() -> Vec<ProcessElement> {
    vec![
        ProcessElement::FirstOrderLag(FirstOrderLag {
            input: PointId(1),
            output: PointId(2),
            time_constant: 2.0,
            initial: 0.0,
        }),
        ProcessElement::SecondOrderLag(SecondOrderLag {
            input: PointId(1),
            output: PointId(2),
            time_constant: 2.0,
            damping_ratio: 0.7,
            initial: 0.0,
        }),
        ProcessElement::Integrator(Integrator {
            input: PointId(1),
            output: PointId(2),
            initial: 0.0,
        }),
        ProcessElement::DeadTime(DeadTime {
            input: PointId(1),
            output: PointId(2),
            delay: 1.5,
            initial: 0.0,
        }),
        ProcessElement::Noise(Noise {
            input: PointId(1),
            output: PointId(2),
            amplitude: 0.5,
            seed: 42,
            initial: 0.0,
        }),
        ProcessElement::BoolFlow(BoolFlow {
            input: PointId(3),
            output: PointId(2),
            on_rate: -0.4,
            off_rate: 0.0,
            initial: 0.0,
        }),
        ProcessElement::FlowSum(FlowSum {
            inputs: vec![PointId(4), PointId(5)],
            output: PointId(2),
            bias: 0.2,
            initial: 0.0,
        }),
        ProcessElement::ScaledFlow(ScaledFlow {
            input: PointId(1),
            output: PointId(2),
            gain: -0.5,
            initial: 0.0,
        }),
        ProcessElement::Threshold(Threshold {
            input: PointId(1),
            output: PointId(6),
            on: 4.9,
            off: 4.7,
            initial: false,
        }),
    ]
}

#[test]
fn schema_covers_every_element_kind_the_merge_accepts() {
    let schema = ProcessElement::json_schema();
    let validator = validator();
    let mut serde_tags = BTreeSet::new();
    for element in one_element_per_kind() {
        let value = serde_json::to_value(&element).unwrap();
        let tag = value
            .as_object()
            .unwrap()
            .keys()
            .next()
            .expect("an element serializes as a single-key object")
            .clone();
        serde_tags.insert(tag);
        let document = serde_json::json!([value]);
        assert!(
            validator.is_valid(&document),
            "the schema rejected {}: {}",
            serde_json::to_string(&document).unwrap(),
            validator
                .iter_errors(&document)
                .map(|error| error.to_string())
                .collect::<Vec<_>>()
                .join("; ")
        );
    }
    // The schema's element vocabulary is exactly the serde tag set —
    // a variant added without its schema entry fails the acceptance
    // above, and a schema kind no variant emits fails here.
    let schema_tags: BTreeSet<String> = schema["$defs"]["element"]["properties"]
        .as_object()
        .unwrap()
        .keys()
        .cloned()
        .collect();
    assert_eq!(serde_tags, schema_tags);
}

#[test]
fn schema_accepts_every_checked_dynamics_document() {
    let validator = validator();
    let root = workspace_root();
    let mut checked = 0;
    for path in dynamics_documents() {
        let relative = path.strip_prefix(&root).unwrap();
        if relative
            .components()
            .any(|component| component.as_os_str() == "invalid")
        {
            // Invalid fixtures carry pinned per-layer verdicts below.
            continue;
        }
        let text = std::fs::read_to_string(&path).unwrap();
        let document: serde_json::Value = serde_json::from_str(&text)
            .unwrap_or_else(|error| panic!("{relative:?} does not parse: {error}"));
        let elements: Vec<ProcessElement> = serde_json::from_str(&text).unwrap_or_else(|error| {
            panic!("{relative:?} does not deserialize as a dynamics document: {error}")
        });
        assert!(
            validator.is_valid(&document),
            "the schema rejected {relative:?}: {}",
            validator
                .iter_errors(&document)
                .map(|error| format!("{error} at {}", error.instance_path()))
                .collect::<Vec<_>>()
                .join("; ")
        );
        // The serde re-serialization — the canonical spelling a
        // regenerated document carries — must validate too.
        let reserialized = serde_json::to_value(&elements).unwrap();
        assert!(
            validator.is_valid(&reserialized),
            "the schema rejected {relative:?}'s reserialization"
        );
        checked += 1;
    }
    assert!(
        checked >= 10,
        "the sweep found {checked} dynamics documents — the fixture corpus shrank"
    );
}

#[test]
fn invalid_dynamics_fixtures_carry_pinned_verdicts() {
    let root = workspace_root();
    // `dynamics_malformed.json` is not JSON at all — neither layer sees
    // a document in it.
    let malformed = std::fs::read_to_string(
        root.join("crates/dcs-plant/fixtures/invalid/dynamics_malformed.json"),
    )
    .unwrap();
    assert!(serde_json::from_str::<serde_json::Value>(&malformed).is_err());
    // The other invalid dynamics fixtures are well-formed element lists
    // whose offenses — unbound points, driven-point conflicts, an
    // element's input and output naming the same point where the legs'
    // kinds differ, an element driving an `out` command point — are
    // merge-layer rules the schema language cannot express: the schema
    // accepts them and `ChannelMap::validate` owns the rejection.
    let validator = validator();
    for name in [
        "dynamics_unbound_point.json",
        "dynamics_unbound_and_conflicting.json",
        "dynamics_self_point.json",
        "dynamics_out_point.json",
    ] {
        let relative = Path::new("crates/dcs-plant/fixtures/invalid").join(name);
        let text = std::fs::read_to_string(root.join(&relative)).unwrap();
        let document: serde_json::Value = serde_json::from_str(&text).unwrap();
        serde_json::from_str::<Vec<ProcessElement>>(&text)
            .unwrap_or_else(|error| panic!("{relative:?} must still deserialize: {error}"));
        assert!(
            validator.is_valid(&document),
            "{relative:?} exercises a merge-layer rule — the schema must accept it"
        );
    }
}

#[test]
fn schema_rejects_a_malformed_element_naming_its_path() {
    // A negative `amplitude` violates the declared bound: the
    // validator's diagnostics must name the offending element — its
    // position in the list and its kind — the way the merge's own
    // rejections do.
    let document = serde_json::json!([
        {"integrator": {"input": 1, "output": 2, "initial": 0.0}},
        {"noise": {
            "input": 2, "output": 3, "amplitude": -0.5, "seed": 7,
            "initial": 0.0
        }}
    ]);
    let validator = validator();
    assert!(!validator.is_valid(&document));
    let diagnostics: Vec<String> = validator
        .iter_errors(&document)
        .map(|error| format!("{error} at {}", error.instance_path()))
        .collect();
    assert!(
        diagnostics
            .iter()
            .any(|error| error.contains("/1/noise/amplitude")),
        "no diagnostic names the malformed element's field: {diagnostics:?}"
    );
}

#[test]
fn schema_rejects_documents_with_structural_violations() {
    let validator = validator();
    let integrator = serde_json::json!({
        "integrator": {"input": 1, "output": 2, "initial": 0.0}
    });
    let cases: &[serde_json::Value] = &[
        // The document is a list, not an object.
        serde_json::json!({"elements": []}),
        // An unknown element tag.
        serde_json::json!([{"pid": {"input": 1, "output": 2, "initial": 0.0}}]),
        // Two element tags on one entry — the externally tagged shape
        // admits exactly one.
        serde_json::json!([{
            "integrator": {"input": 1, "output": 2, "initial": 0.0},
            "noise": {"input": 1, "output": 2, "amplitude": 0.0, "seed": 0, "initial": 0.0}
        }]),
        // An empty element entry.
        serde_json::json!([{}]),
        // A missing required field.
        serde_json::json!([{"integrator": {"input": 1, "output": 2}}]),
        // An unknown field serde would silently ignore.
        {
            let mut element = integrator.clone();
            element["integrator"]["initials"] = serde_json::json!(1.0);
            serde_json::json!([element])
        },
        // A non-positive `time_constant`, `damping_ratio`, or `delay`
        // and a negative `amplitude` — the bounds the schema writes
        // down for the merge.
        serde_json::json!([{"first_order_lag": {
            "input": 1, "output": 2, "time_constant": 0.0, "initial": 0.0
        }}]),
        serde_json::json!([{"second_order_lag": {
            "input": 1, "output": 2, "time_constant": 1.0,
            "damping_ratio": -0.5, "initial": 0.0
        }}]),
        serde_json::json!([{"dead_time": {
            "input": 1, "output": 2, "delay": -1.0, "initial": 0.0
        }}]),
        serde_json::json!([{"noise": {
            "input": 1, "output": 2, "amplitude": -0.1, "seed": 0,
            "initial": 0.0
        }}]),
        // A `threshold`'s `initial` is a `bool`; every other variant's
        // is a number.
        serde_json::json!([{"threshold": {
            "input": 1, "output": 2, "on": 4.9, "off": 4.7,
            "initial": 0.0
        }}]),
        serde_json::json!([{"integrator": {
            "input": 1, "output": 2, "initial": true
        }}]),
        // `flow_sum`'s optional `bias` admits omission, not `null`.
        serde_json::json!([{"flow_sum": {
            "inputs": [1], "output": 2, "bias": null, "initial": 0.0
        }}]),
        // A `seed` outside u64 — written past u64::MAX, the lexical
        // spelling the serde reader also rejects as out of range.
        serde_json::from_str(
            r#"[{"noise": {"input": 1, "output": 2, "amplitude": 0.0,
                "seed": 18446744073709551616, "initial": 0.0}}]"#,
        )
        .unwrap(),
        // A point field that is no integer id.
        serde_json::json!([{"integrator": {
            "input": "in", "output": 2, "initial": 0.0
        }}]),
        serde_json::json!([{"integrator": {
            "input": -1, "output": 2, "initial": 0.0
        }}]),
        // Identical elements — they necessarily drive the same output,
        // the conflict the merge rejects.
        serde_json::json!([
            {"integrator": {"input": 1, "output": 2, "initial": 0.0}},
            {"integrator": {"input": 1, "output": 2, "initial": 0.0}}
        ]),
    ];
    for (index, document) in cases.iter().enumerate() {
        assert!(
            !validator.is_valid(document),
            "case {index} unexpectedly valid: {document}"
        );
    }
}

#[test]
fn schema_accepts_the_document_edges_the_merge_accepts() {
    let validator = validator();
    let cases: &[serde_json::Value] = &[
        // The empty declaration list merges to the untouched map.
        serde_json::json!([]),
        // `flow_sum`'s `bias` may be omitted — it deserializes as zero —
        // and an empty `inputs` declares exactly a constant.
        serde_json::json!([{"flow_sum": {
            "inputs": [1, 2], "output": 3, "initial": 0.0
        }}]),
        serde_json::json!([{"flow_sum": {
            "inputs": [], "output": 3, "bias": -0.25, "initial": 0.0
        }}]),
        // Signed rates and gains — a pump's draw — are legal.
        serde_json::json!([{"bool_flow": {
            "input": 1, "output": 2, "on_rate": -0.4, "off_rate": 0.0,
            "initial": 0.0
        }}]),
        serde_json::json!([{"scaled_flow": {
            "input": 1, "output": 2, "gain": -0.5, "initial": 0.0
        }}]),
        // A falling (low-side) trip: `on < off`.
        serde_json::json!([{"threshold": {
            "input": 1, "output": 2, "on": 0.9, "off": 1.1,
            "initial": true
        }}]),
        // A zero `amplitude` passes the input through unchanged.
        serde_json::json!([{"noise": {
            "input": 1, "output": 2, "amplitude": 0.0, "seed": 0,
            "initial": 0.0
        }}]),
    ];
    for (index, document) in cases.iter().enumerate() {
        assert!(
            validator.is_valid(document),
            "case {index} unexpectedly invalid: {}",
            validator
                .iter_errors(document)
                .map(|error| error.to_string())
                .collect::<Vec<_>>()
                .join("; ")
        );
    }
}
