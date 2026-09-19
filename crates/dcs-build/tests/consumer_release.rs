//! The consumer-release proof (decisions 79–81): a crate outside this
//! workspace resolves the supported engineering surface with no `path`
//! dependency into the checkout — `git` dependencies pinned to an
//! immutable revision of this repository — composes a minimal plant
//! model through `dcs-build`, emits it byte-deterministically, and
//! passes the released tooling: `dcs-model validate`, `dcs-model lint`,
//! and `dcs-controller --check`. The upgrade half of the compatibility
//! policy — the same-minor repin — is proven by the sibling
//! `consumer_upgrade` target over the same mechanics.
//!
//! Run alone from a clean checkout:
//!
//! ```sh
//! cargo test -p dcs-build --test consumer_release
//! ```
//!
//! Every failure the check can land is a named diagnostic, documented
//! in `docs/release-contract.md`:
//!
//! - `pin-unresolvable` — the pinned source does not resolve: an
//!   unfetchable ref or tag, or a revision whose crates satisfy no
//!   declared release requirement.
//! - `surface-incompatible` — the release crates resolved but the
//!   consumer's use of the supported API fails to compile.
//! - `path-dependency-leak` — the consumer's lockfile records a path
//!   source for a released crate: the proof of path-free resolution
//!   failed.
//! - `emit-nondeterministic` — two emission runs produced different
//!   bytes.
//! - `tooling-rejected` — the released tooling refused the emitted
//!   model.

mod common;

use common::{
    PIN_UNRESOLVABLE, Scratch, assert_git_sourced, build, head_rev, released_tooling, resolve,
};

#[test]
fn consumer_resolves_composes_emits_and_passes_released_tooling() {
    let scratch = Scratch::pinned(&format!("rev = \"{}\"", head_rev()));

    // Resolve, compile, and prove the pin carried no path source.
    resolve(&scratch).unwrap_or_else(|diagnostic| panic!("{diagnostic}"));
    build(&scratch).unwrap_or_else(|diagnostic| panic!("{diagnostic}"));
    assert_git_sourced(&scratch);

    // Emit deterministically, then run the released tooling over it.
    let emitted = scratch.emit();
    let model_path = scratch.dir.join("model.json");
    std::fs::write(&model_path, &emitted).unwrap();
    released_tooling(&model_path);
}

#[test]
fn an_unresolvable_or_incompatible_pin_fails_with_the_named_diagnostic() {
    // A revision that does not exist: the remote refuses it.
    let missing = Scratch::pinned(&format!("rev = \"{}\"", "0".repeat(40)));
    let diagnostic = resolve(&missing).unwrap_err();
    assert!(diagnostic.contains(PIN_UNRESOLVABLE), "{diagnostic}");

    // A revision that exists but whose crates satisfy no declared
    // release requirement: the pin resolves the wrong release.
    let incompatible = Scratch::pinned(&format!("rev = \"{}\", version = \">=99\"", head_rev()));
    let diagnostic = resolve(&incompatible).unwrap_err();
    assert!(diagnostic.contains(PIN_UNRESOLVABLE), "{diagnostic}");
}
