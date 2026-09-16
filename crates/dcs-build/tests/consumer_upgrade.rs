//! The consumer-upgrade proof (decision 80's compatibility policy): the
//! same scratch consumer as `consumer_release.rs`, materialized pinned
//! at the revision `docs/release-contract.md` records for `v0.1.0`,
//! then repinned — its own source untouched — to the checkout's `HEAD`,
//! a later commit in the same minor series. Both pins run the identical
//! resolve/build/deterministic-emit/released-tooling pipeline green,
//! and the two pins must emit byte-identical model documents: a repin
//! inside a minor series is a drop-in upgrade, and a divergence is
//! itself the signal the policy asks a release record to name.
//!
//! Run alone from a clean checkout:
//!
//! ```sh
//! cargo test -p dcs-build --test consumer_upgrade
//! ```
//!
//! Beyond `consumer_release`'s diagnostics this check names two more,
//! documented in `docs/release-contract.md`:
//!
//! - `emit-divergent` — the unchanged consumer source emitted different
//!   model bytes under the repinned revision: the same-minor repin was
//!   not the drop-in upgrade the policy promises.
//! - `crossing-unrefused` — an incompatible crossing the contract names
//!   was not refused: the released tooling accepted a document outside
//!   `MODEL_VERSION`, or a pin resolved that must not.

mod common;

use std::process::Command;

use common::{
    PIN_UNRESOLVABLE, Scratch, assert_git_sourced, build, head_rev, released_tool,
    released_tooling, resolve,
};

/// Named diagnostics; see the file header and `docs/release-contract.md`.
const EMIT_DIVERGENT: &str = "emit-divergent";
const CROSSING_UNREFUSED: &str = "crossing-unrefused";

/// The revision `docs/release-contract.md` records for `v0.1.0`: the
/// release tag once it exists in this checkout, and until then the
/// commit that landed the contract — the tag's recorded target.
fn release_rev() -> String {
    let tag = Command::new("git")
        .args(["rev-parse", "--verify", "--quiet", "v0.1.0^{commit}"])
        .current_dir(common::root())
        .output()
        .expect("git rev-parse runs");
    if tag.status.success() {
        return String::from_utf8(tag.stdout).unwrap().trim().to_owned();
    }
    let output = Command::new("git")
        .args([
            "log",
            "--diff-filter=A",
            "--format=%H",
            "-1",
            "--",
            "docs/release-contract.md",
        ])
        .current_dir(common::root())
        .output()
        .expect("git log runs");
    assert!(
        output.status.success(),
        "git log for the contract commit failed"
    );
    let rev = String::from_utf8(output.stdout).unwrap().trim().to_owned();
    assert!(
        !rev.is_empty(),
        "no commit in this checkout landed docs/release-contract.md"
    );
    rev
}

/// The pipeline the contract promises runs green under any same-minor
/// pin: resolve, compile, prove the pin carried no path source, emit
/// deterministically, and pass the released tooling. Returns the
/// emitted model bytes for cross-pin comparison.
fn run_pipeline(scratch: &Scratch, label: &str) -> Vec<u8> {
    resolve(scratch).unwrap_or_else(|diagnostic| panic!("{diagnostic}"));
    build(scratch).unwrap_or_else(|diagnostic| panic!("{diagnostic}"));
    assert_git_sourced(scratch);
    let emitted = scratch.emit();
    let model_path = scratch.dir.join(format!("model-{label}.json"));
    std::fs::write(&model_path, &emitted).unwrap();
    released_tooling(&model_path);
    emitted
}

#[test]
fn a_repin_within_the_minor_series_is_a_drop_in_upgrade() {
    let scratch = Scratch::pinned(&format!("rev = \"{}\"", release_rev()));
    let released = run_pipeline(&scratch, "released");

    // The upgrade: repin the unchanged consumer source to the
    // checkout's HEAD, a later commit in the same minor series.
    scratch.repin(&format!("rev = \"{}\"", head_rev()));
    let upgraded = run_pipeline(&scratch, "upgraded");

    // Byte-identical emission across the repin: a divergence is the
    // signal the compat policy asks a release record to name, and must
    // fail named rather than pass silently.
    assert_eq!(
        released, upgraded,
        "{EMIT_DIVERGENT}: the unchanged consumer source emitted \
         different model bytes under the repinned revision"
    );
}

#[test]
fn an_unsupported_model_version_is_refused_with_the_named_diagnostic() {
    let scratch = Scratch::pinned(&format!("rev = \"{}\"", head_rev()));
    resolve(&scratch).unwrap_or_else(|diagnostic| panic!("{diagnostic}"));
    build(&scratch).unwrap_or_else(|diagnostic| panic!("{diagnostic}"));
    let emitted = scratch.emit();

    // Doctor the emitted document one version past MODEL_VERSION.
    let mut document: serde_json::Value = serde_json::from_slice(&emitted).unwrap();
    let supported = document["version"]
        .as_u64()
        .expect("the emitted document declares a version");
    let found = supported + 1;
    document["version"] = found.into();
    let doctored = scratch.dir.join("model-doctored.json");
    std::fs::write(&doctored, serde_json::to_vec_pretty(&document).unwrap()).unwrap();

    // The released validator must refuse it, naming the found and
    // supported versions.
    let path = doctored.display().to_string();
    let output = released_tool("dcs-model", &["validate", &path]);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        !output.status.success(),
        "{CROSSING_UNREFUSED}: `dcs-model validate` accepted a document at \
         version {found}:\n{stderr}"
    );
    assert!(
        stderr.contains(&format!("unsupported model version {found}"))
            && stderr.contains(&format!("accepts {supported}")),
        "the refusal must name the found and supported versions, got:\n{stderr}"
    );
}

#[test]
fn unresolvable_pins_fail_with_the_named_diagnostic() {
    // A tag the remote does not carry: the release name is unresolvable.
    let missing_tag = Scratch::pinned("tag = \"no-such-release\"");
    match resolve(&missing_tag) {
        Err(diagnostic) => assert!(diagnostic.contains(PIN_UNRESOLVABLE), "{diagnostic}"),
        Ok(()) => panic!("{CROSSING_UNREFUSED}: tag `no-such-release` resolved"),
    }

    // A revision that exists but whose crates satisfy no declared
    // release requirement: the pin resolves the wrong release.
    let excluded = Scratch::pinned(&format!("rev = \"{}\", version = \">=99\"", head_rev()));
    match resolve(&excluded) {
        Err(diagnostic) => assert!(diagnostic.contains(PIN_UNRESOLVABLE), "{diagnostic}"),
        Ok(()) => panic!("{CROSSING_UNREFUSED}: a `version = \">=99\"` pin resolved"),
    }
}
