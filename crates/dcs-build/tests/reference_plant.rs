//! The independent-consumer proof (decision 79): the
//! `reference-plant/` tree in this repository is a verbatim-publishable
//! consumer repository. This test materializes it into a scratch
//! directory *outside* the workspace, rewrites only the dependency
//! remote to a `file://` stand-in for the published origin — the
//! recorded `rev` pin untouched — and runs the tree's own `ci/check.sh`
//! end to end: resolve, build, git-only lockfile sources,
//! byte-identical emit against the checked-in artifacts,
//! released-tooling acceptance, the manifest fingerprint check, and the
//! deterministic scripted simulation.
//!
//! Run alone from a clean checkout:
//!
//! ```sh
//! cargo test -p dcs-build --test reference_plant
//! ```
//!
//! Every failure the template's check can land is a named diagnostic
//! from `docs/release-contract.md` — this test surfaces them verbatim —
//! and the negative cases prove the new stage names the template
//! introduces: `stale-artifact`, `manifest-fingerprint-mismatch`, and
//! `scenario-failed`.

mod common;

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use common::{CARGO, repo_url, root};

/// The remote the published tree records — the string the materialized
/// copy's `Cargo.toml` rewrites to the `file://` stand-in.
const PUBLISHED_REMOTE: &str = "https://github.com/Jan-Kaspar1/dcs.git";

/// The workspace's build target directory, resolved through cargo so a
/// `CARGO_TARGET_DIR` override is honored.
fn target_dir() -> PathBuf {
    let output = Command::new(CARGO)
        .args(["metadata", "--format-version", "1", "--no-deps"])
        .current_dir(root())
        .output()
        .expect("cargo metadata runs");
    assert!(output.status.success(), "cargo metadata failed");
    let metadata: serde_json::Value =
        serde_json::from_slice(&output.stdout).expect("cargo metadata is JSON");
    PathBuf::from(metadata["target_directory"].as_str().unwrap())
}

/// Ensures the released tooling's local stand-ins — `dcs-model`,
/// `dcs-controller`, and `dcs-plant-server` — are built for the check's
/// `DCS_TOOLS` substitution.
fn build_tools() -> PathBuf {
    let output = Command::new(CARGO)
        .args([
            "build",
            "--quiet",
            "-p",
            "dcs-model",
            "-p",
            "dcs-controller",
            "-p",
            "dcs-plant",
        ])
        .current_dir(root())
        .output()
        .expect("cargo build of the released tooling runs");
    assert!(
        output.status.success(),
        "building the released tooling failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    target_dir().join("debug")
}

/// Copies `src` into `dst` recursively.
fn copy_tree(src: &Path, dst: &Path) {
    std::fs::create_dir_all(dst).unwrap();
    for entry in std::fs::read_dir(src).unwrap() {
        let entry = entry.unwrap();
        let target = dst.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            copy_tree(&entry.path(), &target);
        } else {
            std::fs::copy(entry.path(), &target).unwrap();
        }
    }
}

/// A materialized copy of `reference-plant/` outside the workspace,
/// removed on drop.
struct Materialized {
    dir: PathBuf,
}

impl Materialized {
    /// Copies the tree and rewrites only the dependency remote to the
    /// `file://` stand-in — the `rev` pin stays exactly as recorded.
    fn new() -> Self {
        let dir = std::env::temp_dir().join(format!(
            "dcs-reference-plant-{}-{:?}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        copy_tree(&root().join("reference-plant"), &dir);
        let manifest = dir.join("Cargo.toml");
        let source = std::fs::read_to_string(&manifest).unwrap();
        assert!(
            source.contains(PUBLISHED_REMOTE),
            "the template no longer pins the published remote"
        );
        std::fs::write(&manifest, source.replace(PUBLISHED_REMOTE, &repo_url())).unwrap();
        Self { dir }
    }

    /// Runs the template's own clean-CI path against the `file://`
    /// stand-in remote and the locally built tooling — the same
    /// substitutions `consumer_release.rs` makes.
    fn check(&self, tools: &Path) -> Output {
        Command::new("bash")
            .arg("ci/check.sh")
            .current_dir(&self.dir)
            .env("DCS_REMOTE", repo_url())
            .env("DCS_TOOLS", tools)
            .env("CARGO_TARGET_DIR", self.dir.join("target"))
            .output()
            .expect("ci/check.sh runs")
    }
}

impl Drop for Materialized {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// The full materialization proof: the template's own clean-CI check
/// passes green outside the workspace — git-only lockfile sources,
/// byte-stable emit matching the checked-in artifacts, released-tooling
/// acceptance, the manifest fingerprint check, and the deterministic
/// scripted simulation.
#[test]
fn the_template_passes_its_own_clean_ci_outside_the_workspace() {
    let tools = build_tools();
    let copy = Materialized::new();
    let output = copy.check(&tools);
    assert!(
        output.status.success(),
        "the template's ci/check.sh failed:\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

/// A checked-in artifact that no longer matches a fresh emit is the
/// `stale-artifact` diagnostic.
#[test]
fn a_checked_in_model_drift_reports_stale_artifact() {
    let tools = build_tools();
    let copy = Materialized::new();
    let model = copy.dir.join("model/plant.json");
    let source = std::fs::read_to_string(&model).unwrap();
    std::fs::write(
        &model,
        source.replacen("\"version\": 1", "\"version\": 0", 1),
    )
    .unwrap();
    let output = copy.check(&tools);
    assert!(!output.status.success(), "a drifted artifact passed");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("stale-artifact"),
        "expected the stale-artifact diagnostic, got:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

/// A manifest fingerprint the emitted model no longer matches is the
/// `manifest-fingerprint-mismatch` diagnostic.
#[test]
fn a_wrong_manifest_fingerprint_reports_the_named_diagnostic() {
    let tools = build_tools();
    let copy = Materialized::new();
    let manifest = copy.dir.join("deploy/manifest.json");
    let source = std::fs::read_to_string(&manifest).unwrap();
    std::fs::write(&manifest, source.replacen("68b", "fff", 1)).unwrap();
    let output = copy.check(&tools);
    assert!(!output.status.success(), "a wrong fingerprint passed");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("manifest-fingerprint-mismatch"),
        "expected the manifest-fingerprint-mismatch diagnostic, got:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

/// A dynamics document the scenario's declared outcomes no longer hold
/// against is the `scenario-failed` diagnostic.
#[test]
fn a_changed_trajectory_reports_scenario_failed() {
    let tools = build_tools();
    let copy = Materialized::new();
    let dynamics = copy.dir.join("model/dynamics.json");
    let source = std::fs::read_to_string(&dynamics).unwrap();
    // The declared inflow stops: the well never fills and the
    // staging legs' expectations fail.
    std::fs::write(
        &dynamics,
        source.replacen("\"off_rate\": 0.25", "\"off_rate\": 0.0", 1),
    )
    .unwrap();
    let output = copy.check(&tools);
    assert!(!output.status.success(), "a broken scenario passed");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("scenario-failed"),
        "expected the scenario-failed diagnostic, got:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
