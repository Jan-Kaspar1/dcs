//! The consumer-release proof (decisions 79–81): a crate outside this
//! workspace resolves the supported engineering surface with no `path`
//! dependency into the checkout — `git` dependencies pinned to an
//! immutable revision of this repository — composes a minimal plant
//! model through `dcs-build`, emits it byte-deterministically, and
//! passes the released tooling: `dcs-model validate`, `dcs-model lint`,
//! and `dcs-controller --check`.
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

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

/// Named diagnostics; see the file header and `docs/release-contract.md`.
const PIN_UNRESOLVABLE: &str = "pin-unresolvable";
const SURFACE_INCOMPATIBLE: &str = "surface-incompatible";
const PATH_LEAK: &str = "path-dependency-leak";
const EMIT_NONDETERMINISTIC: &str = "emit-nondeterministic";
const TOOLING_REJECTED: &str = "tooling-rejected";

const CARGO: &str = env!("CARGO");

/// The consumer crate's emit entry point: the smallest meaningful plant
/// — a writable internal setpoint and a raw level feeding a scaling
/// block and a PID that drives one field output — composed entirely
/// through the supported `dcs-build` surface, printed as the canonical
/// document. `dcs_model` is named directly as well as transitively:
/// the released model crate is part of the pinned set.
const CONSUMER_MAIN: &str = r#"
use dcs_build::specs::{AnalogInputSpec, PidSpec};
use dcs_build::{parameters, Direction, PlantBuilder, PointId, SignalId, Value};

fn main() {
    let mut plant = PlantBuilder::new();

    let sim = plant.device("sim").id;
    let level_raw = plant.channel::<f64>(sim, "level-raw", Direction::In);
    let valve = plant.channel::<f64>(sim, "valve", Direction::Out);

    let sp = plant.internal_input::<f64>(PointId(10), 50.0, true);
    let level = plant.field_input::<f64>(PointId(11), level_raw, false);
    let cmd = plant.field_output::<f64>(PointId(12), valve);

    plant
        .signal(SignalId(100), "level-setpoint", sp)
        .unit("%")
        .description("Loop level setpoint")
        .group("loop");
    plant
        .signal(SignalId(101), "level-raw", level)
        .unit("mA")
        .description("Raw level measurement")
        .group("loop");
    plant
        .signal(SignalId(102), "valve-command", cmd)
        .unit("%")
        .description("Inlet valve command")
        .group("loop");

    let ai = plant.add(AnalogInputSpec::<f64>::new(parameters([
        ("raw_min", Value::Float(4.0)),
        ("raw_max", Value::Float(20.0)),
        ("eng_min", Value::Float(0.0)),
        ("eng_max", Value::Float(100.0)),
    ])));
    let pid = plant.add(PidSpec::new(parameters([
        ("kp", Value::Float(0.1)),
        ("ki", Value::Float(0.1)),
        ("kd", Value::Float(0.0)),
        ("dt", Value::Float(0.1)),
        ("out_min", Value::Float(4.0)),
        ("out_max", Value::Float(20.0)),
    ])));

    plant.connect(sp, pid.sp);
    plant.connect(level, ai.raw);
    plant.connect(ai.out, pid.pv);
    plant.connect(&pid.out, cmd);

    let model = plant.build().expect("the reference consumer composes");
    assert_eq!(model.version, dcs_model::MODEL_VERSION);
    println!("{}", serde_json::to_string_pretty(&model).unwrap());
}
"#;

/// The workspace root — two levels up from `crates/dcs-build`.
fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(2)
        .unwrap()
        .to_path_buf()
}

/// The repository's own URL as a `file://` remote: the check's stand-in
/// for the published origin a real consumer pins.
fn repo_url() -> String {
    format!("file://{}", root().display())
}

/// The pinned revision the positive case resolves: the checkout's own
/// `HEAD` commit — the immutable-rev pin the contract records beside
/// release tags.
fn head_rev() -> String {
    let output = Command::new("git")
        .args(["rev-parse", "HEAD"])
        .current_dir(root())
        .output()
        .expect("git rev-parse runs");
    assert!(output.status.success(), "git rev-parse HEAD failed");
    String::from_utf8(output.stdout).unwrap().trim().to_owned()
}

/// A throwaway consumer crate outside the workspace, removed on drop.
struct Scratch {
    dir: PathBuf,
}

impl Scratch {
    /// Writes the consumer crate pinned by `pin` — a cargo dependency
    /// specifier fragment like `rev = "…"` or `tag = "…"`, optionally
    /// with `version = "…"` — against this repository as the remote.
    fn pinned(pin: &str) -> Self {
        let dir = std::env::temp_dir().join(format!(
            "dcs-consumer-release-{}-{:?}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let src = dir.join("src");
        std::fs::create_dir_all(&src).unwrap();
        let url = repo_url();
        std::fs::write(
            dir.join("Cargo.toml"),
            format!(
                "\
[package]
name = \"dcs-consumer-check\"
version = \"0.0.0\"
edition = \"2021\"

[dependencies]
dcs-build = {{ git = \"{url}\", {pin} }}
dcs-model = {{ git = \"{url}\", {pin} }}
serde_json = \"1\"
"
            ),
        )
        .unwrap();
        std::fs::write(src.join("main.rs"), CONSUMER_MAIN).unwrap();
        // The consumer pins the same toolchain the release declares.
        std::fs::copy(
            root().join("rust-toolchain.toml"),
            dir.join("rust-toolchain.toml"),
        )
        .unwrap();
        Self { dir }
    }

    /// Runs `cargo` in the consumer crate with an isolated target dir.
    fn cargo(&self, args: &[&str]) -> Output {
        Command::new(CARGO)
            .args(args)
            .current_dir(&self.dir)
            .env("CARGO_TARGET_DIR", self.dir.join("target"))
            .output()
            .unwrap_or_else(|error| panic!("cargo {args:?} failed to spawn: {error}"))
    }

    /// The emitted model written by two deterministic runs.
    fn emit(&self) -> Vec<u8> {
        let first = self.cargo(&["run", "--quiet"]);
        assert!(
            first.status.success(),
            "consumer emit failed: {}",
            String::from_utf8_lossy(&first.stderr)
        );
        let second = self.cargo(&["run", "--quiet"]);
        assert!(
            second.status.success(),
            "consumer emit failed: {}",
            String::from_utf8_lossy(&second.stderr)
        );
        assert_eq!(
            first.stdout, second.stdout,
            "{EMIT_NONDETERMINISTIC}: two emission runs differ"
        );
        first.stdout
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// Resolution alone: `cargo fetch` resolves and fetches every
/// dependency without compiling. Its failure is the contract's
/// `pin-unresolvable` diagnostic — an unfetchable ref or a revision
/// whose crates satisfy no declared requirement.
fn resolve(scratch: &Scratch) -> Result<(), String> {
    let output = scratch.cargo(&["fetch"]);
    if output.status.success() {
        Ok(())
    } else {
        Err(format!(
            "{PIN_UNRESOLVABLE}: {}",
            String::from_utf8_lossy(&output.stderr)
        ))
    }
}

/// The consumer build: a resolved pin whose supported API does not
/// compile is the `surface-incompatible` diagnostic.
fn build(scratch: &Scratch) -> Result<(), String> {
    let output = scratch.cargo(&["build", "--quiet"]);
    if output.status.success() {
        Ok(())
    } else {
        Err(format!(
            "{SURFACE_INCOMPATIBLE}: {}",
            String::from_utf8_lossy(&output.stderr)
        ))
    }
}

/// Asserts the consumer's lockfile records the release crates from the
/// pinned git source — never a `path` source into the checkout.
fn assert_git_sourced(scratch: &Scratch) {
    let lock = std::fs::read_to_string(scratch.dir.join("Cargo.lock")).unwrap();
    for name in ["dcs-build", "dcs-core", "dcs-model"] {
        let section = lock
            .split("name = \"")
            .find(|part| part.starts_with(&format!("{name}\"")))
            .unwrap_or_else(|| panic!("{name} missing from the consumer lockfile"));
        assert!(
            section.contains("source = \"git+file://"),
            "{PATH_LEAK}: {name} did not resolve from the pinned git \
             source:\n{section}"
        );
    }
    assert!(
        !lock.contains("source = \"path+"),
        "{PATH_LEAK}: the consumer lockfile records a path source:\n{lock}"
    );
}

/// Runs the released tooling against the emitted model through the
/// workspace's own binaries — the local stand-in for the contract's
/// `cargo install --git … --tag …` mechanism — each command's failure
/// reported as `tooling-rejected`.
fn released_tooling(model: &Path) {
    let commands: [Vec<String>; 3] = [
        vec![
            "run".into(),
            "--quiet".into(),
            "-p".into(),
            "dcs-model".into(),
            "--".into(),
            "validate".into(),
            model.display().to_string(),
        ],
        vec![
            "run".into(),
            "--quiet".into(),
            "-p".into(),
            "dcs-model".into(),
            "--".into(),
            "lint".into(),
            model.display().to_string(),
        ],
        vec![
            "run".into(),
            "--quiet".into(),
            "-p".into(),
            "dcs-controller".into(),
            "--".into(),
            model.display().to_string(),
            "--check".into(),
        ],
    ];
    let mut outputs = Vec::new();
    for args in &commands {
        let output = Command::new(CARGO)
            .args(args)
            .current_dir(root())
            .output()
            .unwrap_or_else(|error| panic!("cargo {args:?} failed to spawn: {error}"));
        assert!(
            output.status.success(),
            "{TOOLING_REJECTED}: cargo {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&output.stderr)
        );
        outputs.push(String::from_utf8(output.stdout).unwrap());
    }
    // The composed model is lint-clean: every point is signaled, every
    // signal carries unit/description/group, the writable point is an
    // internal setpoint, and every channel is bound.
    assert!(
        outputs[1].contains("no findings"),
        "expected a lint-clean model, got:\n{}",
        outputs[1]
    );
    assert!(
        outputs[2].contains("check ok:"),
        "expected the --check summary, got:\n{}",
        outputs[2]
    );
}

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
