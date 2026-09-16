//! The independent-consumer proof (decision 79): the
//! `reference-plant/` tree in this repository is a verbatim-publishable
//! consumer repository. This test materializes it into a scratch
//! directory *outside* the workspace, rewrites only the dependency
//! remote to a `file://` stand-in for the published origin — the
//! recorded `rev` pin untouched, with the stand-in seeded to serve
//! exactly that commit — and runs the tree's own `ci/check.sh`
//! end to end: resolve, build, git-only lockfile sources,
//! byte-identical emit against the checked-in artifacts,
//! released-tooling acceptance, the manifest fingerprint check, the
//! deterministic scripted simulation, the served operator surface —
//! the signal index, page, snapshot descriptors, and journal asserted
//! against the emitted model's declaration — the `consumers` stage,
//! which replays that driven run under each consumer schedule (no UI,
//! polling, a stalled reader, churn, malformed/flooded traffic, a UI
//! process restart) requiring identical digests, and the `upgrade`
//! stage, which repins the materialized tree to the checkout's `HEAD`
//! (seeded into the stand-in beside the recorded rev) and re-runs the
//! full pipeline under the repin.
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
use std::sync::Mutex;

use common::{CARGO, PIN_UNRESOLVABLE, head_rev, root};

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

/// The `rev = "…"` pin the template's manifest records for the release
/// crates — the object the stand-in remote must serve.
fn pinned_rev(dir: &Path) -> String {
    let manifest = std::fs::read_to_string(dir.join("Cargo.toml")).unwrap();
    for line in manifest.lines() {
        if let Some(start) = line.find("rev = \"") {
            let rest = &line[start + "rev = \"".len()..];
            if let Some(end) = rest.find('"') {
                return rest[..end].to_owned();
            }
        }
    }
    panic!("the template's Cargo.toml records no rev pin");
}

/// Runs `git args` in `dir`, asserting success.
fn git(dir: &Path, args: &[&str]) {
    let output = Command::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .expect("git runs");
    assert!(
        output.status.success(),
        "git {} failed: {}",
        args.join(" "),
        String::from_utf8_lossy(&output.stderr)
    );
}

/// Ensures the checkout's object store contains `rev`. CI checkouts
/// are shallow (`actions/checkout` fetches at depth 1), so the
/// recorded release commit may be absent; fetch it from `origin` — the
/// published origin itself — when the store lacks it.
fn ensure_commit(rev: &str) {
    static FETCH_LOCK: Mutex<()> = Mutex::new(());
    let _guard = FETCH_LOCK.lock().unwrap();
    let present = Command::new("git")
        .args(["cat-file", "-e", &format!("{rev}^{{commit}}")])
        .current_dir(root())
        .output()
        .expect("git cat-file runs");
    if present.status.success() {
        return;
    }
    for remote in ["origin", PUBLISHED_REMOTE] {
        let fetch = Command::new("git")
            .args(["fetch", "--depth", "1", remote, rev])
            .current_dir(root())
            .output()
            .expect("git fetch runs");
        if fetch.status.success() {
            return;
        }
    }
    panic!("{PIN_UNRESOLVABLE}: no remote could serve the pinned rev {rev}");
}

/// A `file://` stand-in for the published origin: a bare repository in
/// the materialized scratch that serves exactly the recorded rev. The
/// workspace checkout alone cannot play the remote in CI — its shallow
/// object store lacks the pinned commit and serves no way to name it —
/// so the stand-in is seeded with that commit, the same object the
/// published origin serves for the recorded rev. The `upgrade` stage's
/// repin target — the checkout's `HEAD`, a later commit in the same
/// minor series — is seeded beside it so the repin resolves.
fn serve_pinned_rev(scratch: &Path) -> String {
    let rev = pinned_rev(scratch);
    ensure_commit(&rev);
    let remote = scratch.join("dcs-remote.git");
    git(scratch, &["init", "--bare", "dcs-remote.git"]);
    // The local transport serves the object directly; the published
    // origin is the fallback when the checkout cannot.
    let fetch = Command::new("git")
        .args(["fetch", "--depth", "1", &root().display().to_string(), &rev])
        .current_dir(&remote)
        .output()
        .expect("git fetch runs");
    if !fetch.status.success() {
        git(&remote, &["fetch", "--depth", "1", PUBLISHED_REMOTE, &rev]);
    }
    git(&remote, &["update-ref", "refs/heads/main", &rev]);
    let head = head_rev();
    git(
        &remote,
        &[
            "fetch",
            "--depth",
            "1",
            &root().display().to_string(),
            &head,
        ],
    );
    git(&remote, &["update-ref", "refs/heads/upgrade", &head]);
    format!("file://{}", remote.display())
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
    remote: String,
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
        let remote = serve_pinned_rev(&dir);
        let manifest = dir.join("Cargo.toml");
        let source = std::fs::read_to_string(&manifest).unwrap();
        assert!(
            source.contains(PUBLISHED_REMOTE),
            "the template no longer pins the published remote"
        );
        std::fs::write(&manifest, source.replace(PUBLISHED_REMOTE, &remote)).unwrap();
        Self { dir, remote }
    }

    /// Runs the template's own clean-CI path against the `file://`
    /// stand-in remote and the locally built tooling — the same
    /// substitutions `consumer_release.rs` makes, plus the upgrade
    /// stage's repin target: the checkout's `HEAD`, a later commit in
    /// the same minor series the stand-in remote also serves.
    fn check(&self, tools: &Path) -> Output {
        Command::new("bash")
            .arg("ci/check.sh")
            .current_dir(&self.dir)
            .env("DCS_REMOTE", &self.remote)
            .env("DCS_TOOLS", tools)
            .env("DCS_UPGRADE_REV", head_rev())
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
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        output.status.success(),
        "the template's ci/check.sh failed:\nstdout:\n{stdout}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    // The consumer-boundary stage ran and held: the same driven run's
    // digest under every consumer schedule, identical across passes —
    // the `file://` stand-in resolving the current revision's tooling.
    assert!(
        stdout.contains("== consumers =="),
        "the consumers stage did not run:\n{stdout}"
    );
    assert!(
        stdout.contains("identical across every schedule and both passes"),
        "the consumer schedules did not produce identical digests:\n{stdout}",
    );
}

/// The `upgrade` stage is the executable assertion of the documented
/// repin upgrade (README §7): under the same `file://`-remote and
/// binary substitutions as the other stages, the stage repins the
/// unchanged tree to the checkout's `HEAD`, proves the emitted
/// `model/plant.json` is byte-identical across the repin
/// (`emit-divergent` stands guard), re-runs the full pipeline under the
/// repin, and refuses the named incompatible crossings — the
/// nonexistent tag (`pin-unresolvable`) and the pin outside the
/// supported `MODEL_VERSION`/`version` window (`crossing-unrefused`).
#[test]
fn the_upgrade_stage_proves_the_repin_and_the_named_crossings() {
    let tools = build_tools();
    let copy = Materialized::new();
    let output = copy.check(&tools);
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        output.status.success(),
        "the template's ci/check.sh failed:\nstdout:\n{stdout}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        stdout.contains("== upgrade =="),
        "the upgrade stage did not run:\n{stdout}"
    );
    assert!(
        stdout.contains("byte-identical emit across the repin"),
        "the repin did not emit byte-identically:\n{stdout}"
    );
    assert!(
        stdout.contains("the full pipeline passes under the repin"),
        "the repinned pipeline did not pass:\n{stdout}"
    );
    assert!(
        stdout.contains("pin-unresolvable") && stdout.contains("outside MODEL_VERSION"),
        "the named incompatible crossings were not exercised:\n{stdout}"
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

/// The `surface` stage's tamper case, at script level. Every edit to
/// the materialized tree that would desynchronize the served surface
/// from the declared one is already caught by an earlier stage — emit
/// proves the checked-in artifacts byte-equal a fresh emit, and the
/// controller serves the same document the stage derives its
/// expectations from — so this exercises the stage's comparison
/// directly: a served index missing a declared writable command point,
/// a writable point served read-only, or the never-shelvable alarm's
/// read-only `shelve` surface served writable each report the named
/// mismatches `ci/check.sh` turns into `surface-mismatch`.
#[test]
fn a_tampered_served_index_reports_named_mismatches() {
    let output = Command::new("python3")
        .arg("-c")
        .arg(
            r#"
import importlib.util
import json

spec = importlib.util.spec_from_file_location("simulate", "ci/simulate.py")
simulate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(simulate)

model = json.load(open("model/plant.json"))
declared = simulate.declared_signal_index(model)

# The untampered index reports no mismatches.
served = json.loads(json.dumps(declared))
assert simulate.index_mismatches(declared, served) == []

# A declared writable command point dropped from the served index is
# named as not served.
served = json.loads(json.dumps(declared))
writable = next(e["point"] for e in served["points"] if e["writable"])
served["points"] = [e for e in served["points"] if e["point"] != writable]
failures = simulate.index_mismatches(declared, served)
assert any(str(writable) in f and "not served" in f for f in failures), failures

# A writable command point served read-only is flagged.
served = json.loads(json.dumps(declared))
entry = next(e for e in served["points"] if e["point"] == writable)
entry["writable"] = False
failures = simulate.index_mismatches(declared, served)
assert any(str(writable) in f and "writable" in f for f in failures), failures

# The never-shelvable high-level alarm's read-only shelve point served
# writable is flagged — the refused shelve surface.
served = json.loads(json.dumps(declared))
entry = next(e for e in served["points"] if e["name"] == "lah-shelve")
entry["writable"] = True
failures = simulate.index_mismatches(declared, served)
assert any("lah-shelve" in f and "writable" in f for f in failures), failures

print("tamper cases report named mismatches")
"#,
        )
        .current_dir(root().join("reference-plant"))
        .output()
        .expect("python3 runs the surface comparison");
    assert!(
        output.status.success(),
        "the surface comparison did not flag the tampered index:\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}
