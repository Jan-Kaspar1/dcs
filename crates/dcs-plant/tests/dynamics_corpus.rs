//! The dynamics document's schema-vs-merge conformance corpus: QA
//! finding `dynamics-schema-merge-differential-battery` (issue #1040)
//! ran every checked-in dynamics document through both document doors —
//! `dcs-plant-server --dynamics-schema`'s emitted schema and
//! `dcs-plant-server <model> --check-dynamics`'s merge — and pinned the
//! verdict pair in `scripts/dyn/manifest.json`. The corpus is the
//! recorded answer to the finding's two rules: a schema rejection
//! beyond the documented unknown-inner-field class must imply a merge
//! rejection, and every schema+merge accept must produce a runnable
//! plant.
//!
//! Each file under `scripts/dyn/cases/` is named
//! `<schema>-<merge>__<name>.json`, the prefix restating the manifest's
//! pinned verdicts, and the manifest's `class` field names the
//! divergence the case exists to pin: the `valid` documents — including
//! the one legal self-point, a lag reading and driving a single `Float`
//! point — the `unknown-inner-field` schema-FAIL/merge-PASS class serde
//! silently ignores, the merge-only classes the schema language cannot
//! express (`unknown-point`, `gate-kind-mismatch`,
//! `contact-kind-mismatch`, `on-equals-off`, `nonfinite-literal`,
//! `float-point-id`, `u64-overflow-id`, `conflicting-driver`,
//! `out-point`, and the `self-point` bool_flow/threshold must-rejects
//! the per-leg kind checks own — see issue #957), and the
//! shared-rejection classes where the schema's approximation of a merge
//! rule is sound (`unknown-element-tag`, `bound-violation`,
//! `duplicate-elements`, `malformed`).

use dcs_core::IoDriver;
use dcs_sim_net::RemoteDriver;
use std::collections::{BTreeSet, HashMap};
use std::io::{BufRead, BufReader, Read};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, Command, ExitStatus, Stdio};

/// The binary under test, built by Cargo alongside the test harness.
const SERVER: &str = env!("CARGO_BIN_EXE_dcs-plant-server");
/// The corpus manifest, relative to the workspace root.
const MANIFEST: &str = "scripts/dyn/manifest.json";
/// The corpus case directory, relative to the workspace root.
const CASES_DIR: &str = "scripts/dyn/cases";

fn workspace_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap()
}

/// One manifest entry: a corpus document, the model its merge runs
/// against, its two pinned verdicts, and the divergence class it pins.
struct Case {
    /// The dynamics document's workspace-relative path.
    file: String,
    /// The plant model's workspace-relative path.
    model: String,
    /// Whether the emitted schema validates the document.
    schema: bool,
    /// Whether `--check-dynamics` merges the document.
    merge: bool,
    /// The divergence class the case pins.
    class: String,
}

/// Reads one pinned verdict — `"pass"` or `"fail"`, nothing else.
fn pinned_verdict(entry: &serde_json::Value, field: &str) -> bool {
    match entry[field].as_str() {
        Some("pass") => true,
        Some("fail") => false,
        other => {
            panic!("{MANIFEST}: field {field:?} must be \"pass\" or \"fail\", found {other:?}")
        }
    }
}

/// The manifest's case list, in file order.
fn corpus() -> Vec<Case> {
    let text = std::fs::read_to_string(workspace_root().join(MANIFEST)).unwrap();
    let manifest: serde_json::Value = serde_json::from_str(&text)
        .unwrap_or_else(|error| panic!("{MANIFEST} does not parse: {error}"));
    manifest["cases"]
        .as_array()
        .unwrap_or_else(|| panic!("{MANIFEST} must carry a \"cases\" list"))
        .iter()
        .map(|entry| Case {
            file: entry["file"]
                .as_str()
                .unwrap_or_else(|| panic!("{MANIFEST}: a case carries no \"file\": {entry}"))
                .to_owned(),
            model: entry["model"]
                .as_str()
                .unwrap_or_else(|| panic!("{MANIFEST}: a case carries no \"model\": {entry}"))
                .to_owned(),
            schema: pinned_verdict(entry, "schema"),
            merge: pinned_verdict(entry, "merge"),
            class: entry["class"]
                .as_str()
                .unwrap_or_else(|| panic!("{MANIFEST}: a case carries no \"class\": {entry}"))
                .to_owned(),
        })
        .collect()
}

/// A running `dcs-plant-server` child: its process, bound address, and
/// stderr stream (held open so the child's later messages never meet a
/// closed pipe).
struct Plant {
    child: Child,
    addr: SocketAddr,
    stderr: BufReader<ChildStderr>,
}

/// Spawns the server with `args` and reads its `listening on` line to
/// learn the bound address — `--listen 127.0.0.1:0` binds an ephemeral
/// port with no reservation race.
fn spawn(args: &[&str]) -> Plant {
    let mut child = Command::new(SERVER)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .expect("dcs-plant-server spawns");
    let mut stderr = BufReader::new(child.stderr.take().unwrap());
    let mut line = String::new();
    if stderr.read_line(&mut line).unwrap() == 0 {
        // The process exited before announcing its address; report why.
        let status = child.wait().unwrap();
        let mut rest = String::new();
        stderr.read_to_string(&mut rest).ok();
        panic!("dcs-plant-server exited {status} before serving: {rest}");
    }
    let addr = line
        .trim()
        .strip_prefix("listening on ")
        .unwrap_or_else(|| panic!("unexpected server output: {line:?}"))
        .parse()
        .expect("the announced address parses");
    Plant {
        child,
        addr,
        stderr,
    }
}

/// Signals the child with SIGTERM and waits for it to exit; a graceful
/// shutdown reports `success`. Drains the child's stderr afterward so a
/// failing wait can be diagnosed.
fn stop(plant: &mut Plant) -> ExitStatus {
    let status = Command::new("kill")
        .args(["-TERM", &plant.child.id().to_string()])
        .status()
        .expect("kill runs");
    assert!(status.success(), "kill could not signal the child");
    let status = plant.child.wait().unwrap();
    if !status.success() {
        let mut rest = String::new();
        plant.stderr.read_to_string(&mut rest).ok();
        panic!("dcs-plant-server exited {status}, stderr: {rest}");
    }
    status
}

/// Rewrites every number literal serde_json's `Value` cannot hold — a
/// magnitude whose `f64` parse is not finite — into a same-sign finite
/// sentinel, so the emitted schema can rule on the document's grammar.
/// Each distinct literal maps to a distinct sentinel, so `uniqueItems`
/// still tells them apart, and every sentinel sits past each bound the
/// emitted schema declares — its numeric keywords all compare within
/// u64 range — so the rewritten document's verdict is the document's
/// own: `1e999` is a `number` far above every bound to any validator,
/// and only the representation changed. String contents are skipped —
/// a literal spelling inside quotes is data, not a number token.
fn finite_sentinels(text: &str) -> String {
    let bytes = text.as_bytes();
    // (start, end) spans of every JSON number token, in text order.
    let mut tokens = Vec::new();
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'"' => {
                index += 1;
                while index < bytes.len() && bytes[index] != b'"' {
                    index += if bytes[index] == b'\\' { 2 } else { 1 };
                }
                index += 1;
            }
            b'-' | b'0'..=b'9' => {
                let start = index;
                index += 1;
                while index < bytes.len()
                    && matches!(bytes[index], b'0'..=b'9' | b'.' | b'e' | b'E' | b'+' | b'-')
                {
                    index += 1;
                }
                if text[start..index]
                    .parse::<f64>()
                    .is_ok_and(|value| !value.is_finite())
                {
                    tokens.push((start, index));
                }
            }
            _ => index += 1,
        }
    }
    // A distinct sentinel per distinct overflowing literal, sign kept:
    // identical literals stay identical for `uniqueItems`, distinct
    // ones stay distinct.
    let mut sentinels: HashMap<String, String> = HashMap::new();
    let mut rewritten = String::with_capacity(text.len());
    let mut cursor = 0;
    for (start, end) in tokens {
        let literal = &text[start..end];
        let ordinal = sentinels.len() + 1;
        let sentinel = sentinels
            .entry(literal.to_owned())
            .or_insert_with(|| format!("{ordinal}e300"));
        rewritten.push_str(&text[cursor..start]);
        if literal.starts_with('-') {
            rewritten.push('-');
        }
        rewritten.push_str(sentinel);
        cursor = end;
    }
    rewritten.push_str(&text[cursor..]);
    rewritten
}

/// Parses a case file into the `Value` the schema validator consumes.
/// serde_json's `Value` cannot hold an out-of-range literal — `1e999`
/// fails the whole parse with "number out of range" — yet such a file
/// is valid JSON grammar: python jsonschema, the reproduction's
/// validator, reads `1e999` as a `number` and the emitted schema
/// accepts it. The sentinel rewrite gives the emitted schema the same
/// document to rule on. `None` when the file is not JSON at all.
fn parse_for_schema(text: &str) -> Option<serde_json::Value> {
    match serde_json::from_str(text) {
        Ok(document) => Some(document),
        Err(error) if error.to_string().contains("number out of range") => {
            serde_json::from_str(&finite_sentinels(text)).ok()
        }
        Err(_) => None,
    }
}

#[test]
fn manifest_covers_the_case_directory_and_the_names_restate_the_pins() {
    let root = workspace_root();
    let cases = corpus();
    assert!(
        cases.len() >= 20,
        "the corpus shrank — {MANIFEST} lists {} cases",
        cases.len()
    );

    // Every manifest entry names a file inside the corpus directory
    // that exists, and the filename's `<schema>-<merge>` prefix
    // restates the pinned verdicts — the name is the pin a reader
    // sees, so a drift between name and manifest fails here.
    let mut listed = BTreeSet::new();
    for case in &cases {
        let file = Path::new(&case.file);
        assert!(
            file.starts_with(CASES_DIR),
            "{}: corpus files live under {CASES_DIR}",
            case.file
        );
        assert!(
            root.join(file).is_file(),
            "{}: listed in {MANIFEST} but missing on disk",
            case.file
        );
        assert!(
            !case.class.is_empty(),
            "{}: carries no divergence class",
            case.file
        );
        assert!(
            root.join(&case.model).is_file(),
            "{}: model {} is missing on disk",
            case.file,
            case.model
        );
        assert!(
            listed.insert(case.file.clone()),
            "{}: listed twice in {MANIFEST}",
            case.file
        );
        let name = file.file_name().unwrap().to_str().unwrap();
        let prefix = format!(
            "{}-{}__",
            if case.schema { "pass" } else { "fail" },
            if case.merge { "pass" } else { "fail" },
        );
        assert!(
            name.starts_with(&prefix),
            "{name}: the filename prefix must restate the pinned verdicts ({prefix}<name>.json)"
        );
    }

    // The directory and the manifest name the same file set — an
    // unlisted case file, or a listed one that vanished, fails here.
    let mut on_disk = BTreeSet::new();
    for entry in std::fs::read_dir(root.join(CASES_DIR)).unwrap().flatten() {
        let path = entry.path();
        if path
            .extension()
            .is_some_and(|extension| extension == "json")
        {
            on_disk.insert(format!(
                "{CASES_DIR}/{}",
                path.file_name().unwrap().to_str().unwrap()
            ));
        }
    }
    assert_eq!(
        listed, on_disk,
        "{MANIFEST} and {CASES_DIR} disagree on the corpus"
    );
}

#[test]
fn each_case_holds_its_pinned_schema_verdict() {
    // The schema layer is the emitted artifact — the same bytes a
    // consumer validates against — not the entry function's in-process
    // copy.
    let output = Command::new(SERVER)
        .arg("--dynamics-schema")
        .output()
        .expect("dcs-plant-server runs");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let emitted: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    let validator =
        jsonschema::validator_for(&emitted).expect("the emitted document must be a usable schema");

    let root = workspace_root();
    for case in corpus() {
        let text = std::fs::read_to_string(root.join(&case.file)).unwrap();
        // A file that is not JSON fails the schema layer outright —
        // the schema has no document to validate — and the merge's
        // parse reports the same file: such a case must pin fail/fail.
        let valid = match parse_for_schema(&text) {
            Some(document) => {
                let valid = validator.is_valid(&document);
                if valid != case.schema {
                    let diagnostics: Vec<String> = validator
                        .iter_errors(&document)
                        .map(|error| format!("{error} at {}", error.instance_path()))
                        .collect();
                    panic!(
                        "{} ({}): schema verdict is {}, pinned {}: {diagnostics:?}",
                        case.file,
                        case.class,
                        if valid { "pass" } else { "fail" },
                        if case.schema { "pass" } else { "fail" },
                    );
                }
                valid
            }
            None => false,
        };
        assert_eq!(
            valid, case.schema,
            "{} ({}): schema verdict drifted from its pin",
            case.file, case.class
        );
    }
}

#[test]
fn each_case_holds_its_pinned_merge_verdict() {
    // The merge layer is the standalone preflight — the same
    // per-element merge-and-validate the serving run applies, with no
    // listener bound.
    let root = workspace_root();
    for case in corpus() {
        let model = root.join(&case.model);
        let file = root.join(&case.file);
        let output = Command::new(SERVER)
            .arg(&model)
            .arg("--check-dynamics")
            .arg(&file)
            .output()
            .expect("dcs-plant-server runs");
        assert_eq!(
            output.status.success(),
            case.merge,
            "{} ({}): --check-dynamics verdict is {}, pinned {}: {}",
            case.file,
            case.class,
            if output.status.success() {
                "pass"
            } else {
                "fail"
            },
            if case.merge { "pass" } else { "fail" },
            String::from_utf8_lossy(&output.stderr)
        );
    }
}

#[test]
fn each_merge_acceptance_serves_a_runnable_plant() {
    // The finding's second rule: a document the schema and the merge
    // both accept — and the unknown-inner-field document the merge
    // accepts past a schema rejection — must serve a plant that steps,
    // not a listener that dies on the first request (the self-point
    // panic the #957 merge checks now refuse). The served path runs
    // the same merge `--check-dynamics` preflights, so the pin is a
    // live claim, step, and read against the bound server.
    let root = workspace_root();
    for case in corpus().into_iter().filter(|case| case.merge) {
        let model = root.join(&case.model);
        let file = root.join(&case.file);
        let mut plant = spawn(&[
            model.to_str().unwrap(),
            "--dynamics",
            file.to_str().unwrap(),
            "--listen",
            "127.0.0.1:0",
        ]);
        let driver = RemoteDriver::connect(plant.addr).unwrap();
        driver.claim_writer(1).unwrap();
        // The step that used to panic inside the driver mutex answers
        // now — and every declared point still reads back a sample.
        driver.step(0.1).unwrap_or_else(|error| {
            panic!("{}: the served plant refused a step: {error}", case.file)
        });
        for info in driver.list_points().unwrap() {
            driver.read(info.point).unwrap_or_else(|error| {
                panic!(
                    "{}: point {} does not read back: {error}",
                    case.file, info.point.0
                )
            });
        }
        assert!(stop(&mut plant).success(), "{}", case.file);
    }
}

#[test]
fn each_merge_rejection_refuses_the_serving_run() {
    // A document the preflight rejects must meet the same rejection at
    // startup on the serving path — the merge is shared, so the run
    // exits nonzero before binding rather than serving a plant whose
    // first step is where the defect lands.
    let root = workspace_root();
    for case in corpus().into_iter().filter(|case| !case.merge) {
        let model = root.join(&case.model);
        let file = root.join(&case.file);
        let output = Command::new(SERVER)
            .arg(&model)
            .arg("--dynamics")
            .arg(&file)
            .arg("--listen")
            .arg("127.0.0.1:0")
            .output()
            .expect("dcs-plant-server runs");
        assert!(
            !output.status.success(),
            "{} ({}): the serving run unexpectedly accepted a merge-rejected document",
            case.file,
            case.class
        );
        assert!(
            !String::from_utf8_lossy(&output.stderr).contains("listening on"),
            "{}: a listener bound before the rejection reported",
            case.file
        );
    }
}
