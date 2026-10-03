//! Shared mechanics for the consumer-contract checks in
//! `consumer_release.rs` and `consumer_upgrade.rs`: a scratch consumer
//! crate outside this workspace pinned against this repository as a
//! `file://` remote (the stand-in for the published origin a real
//! consumer pins), the resolve/build/deterministic-emit pipeline, the
//! released-tooling runs, and the named diagnostics
//! `docs/release-contract.md` records.

// Each check target uses the subset of these mechanics its cases need.
#![allow(dead_code)]

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

/// Named diagnostics; see `docs/release-contract.md`.
pub(crate) const PIN_UNRESOLVABLE: &str = "pin-unresolvable";
pub(crate) const SURFACE_INCOMPATIBLE: &str = "surface-incompatible";
pub(crate) const PATH_LEAK: &str = "path-dependency-leak";
pub(crate) const EMIT_NONDETERMINISTIC: &str = "emit-nondeterministic";
pub(crate) const TOOLING_REJECTED: &str = "tooling-rejected";

pub(crate) const CARGO: &str = env!("CARGO");

/// The consumer crate's emit entry point: the smallest meaningful plant
/// — a writable internal setpoint and a raw level feeding a scaling
/// block and a PID that drives one field output — composed entirely
/// through the supported `dcs-build` surface, printed as the canonical
/// document. `dcs_model` is named directly as well as transitively:
/// the released model crate is part of the pinned set.
pub(crate) const CONSUMER_MAIN: &str = r#"
use dcs_build::specs::{AnalogInputSpec, PidSpec};
use dcs_build::{parameters, Direction, PlantBuilder, PointId, SignalId, Value};

fn main() {
    let mut plant = PlantBuilder::new();

    let sim = plant.device("sim").id;
    let level_raw = plant.channel::<f64>(sim, "level-raw", Direction::In);
    let valve = plant.channel::<f64>(sim, "valve", Direction::Out);

    let sp = plant.internal_input::<f64>(PointId(10), 50.0, true);
    // The field input declares its freshness budget — the model's
    // lint names channel-bound `in` points that leave it unset.
    let level = plant.field_input_stale_after::<f64>(PointId(11), level_raw, false, 5);
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
pub(crate) fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(2)
        .unwrap()
        .to_path_buf()
}

/// The repository's own URL as a `file://` remote: the check's stand-in
/// for the published origin a real consumer pins.
pub(crate) fn repo_url() -> String {
    format!("file://{}", root().display())
}

/// The checkout's own `HEAD` commit — the immutable-rev pin the
/// contract records beside release tags.
pub(crate) fn head_rev() -> String {
    let output = Command::new("git")
        .args(["rev-parse", "HEAD"])
        .current_dir(root())
        .output()
        .expect("git rev-parse runs");
    assert!(output.status.success(), "git rev-parse HEAD failed");
    String::from_utf8(output.stdout).unwrap().trim().to_owned()
}

/// The consumer crate's manifest: `dcs-build` and `dcs-model` pinned by
/// `pin` — a cargo dependency specifier fragment like `rev = "…"` or
/// `tag = "…"`, optionally with `version = "…"` — against this
/// repository as the remote.
fn manifest(pin: &str) -> String {
    let url = repo_url();
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
    )
}

/// A throwaway consumer crate outside the workspace, removed on drop.
pub(crate) struct Scratch {
    pub(crate) dir: PathBuf,
}

impl Scratch {
    /// Writes the consumer crate pinned by `pin`.
    pub(crate) fn pinned(pin: &str) -> Self {
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
        std::fs::write(dir.join("Cargo.toml"), manifest(pin)).unwrap();
        std::fs::write(src.join("main.rs"), CONSUMER_MAIN).unwrap();
        // The consumer pins the same toolchain the release declares.
        std::fs::copy(
            root().join("rust-toolchain.toml"),
            dir.join("rust-toolchain.toml"),
        )
        .unwrap();
        Self { dir }
    }

    /// Rewrites the dependency pins in place: the consumer's own source
    /// is untouched, only the release it resolves changes.
    pub(crate) fn repin(&self, pin: &str) {
        std::fs::write(self.dir.join("Cargo.toml"), manifest(pin)).unwrap();
    }

    /// Runs `cargo` in the consumer crate with an isolated target dir.
    pub(crate) fn cargo(&self, args: &[&str]) -> Output {
        Command::new(CARGO)
            .args(args)
            .current_dir(&self.dir)
            .env("CARGO_TARGET_DIR", self.dir.join("target"))
            .output()
            .unwrap_or_else(|error| panic!("cargo {args:?} failed to spawn: {error}"))
    }

    /// The emitted model written by two deterministic runs.
    pub(crate) fn emit(&self) -> Vec<u8> {
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
pub(crate) fn resolve(scratch: &Scratch) -> Result<(), String> {
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
pub(crate) fn build(scratch: &Scratch) -> Result<(), String> {
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
pub(crate) fn assert_git_sourced(scratch: &Scratch) {
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

/// Runs one released tool — `cargo run -p <package> -- <args>` — at the
/// checkout root: the local stand-in for the contract's
/// `cargo install --git … --tag …` mechanism. The caller judges the
/// result, so both accepted and refused crossings are expressible.
pub(crate) fn released_tool(package: &str, args: &[&str]) -> Output {
    let mut command: Vec<String> = vec![
        "run".into(),
        "--quiet".into(),
        "-p".into(),
        package.into(),
        "--".into(),
    ];
    command.extend(args.iter().map(|arg| (*arg).to_owned()));
    Command::new(CARGO)
        .args(&command)
        .current_dir(root())
        .output()
        .unwrap_or_else(|error| panic!("cargo {command:?} failed to spawn: {error}"))
}

/// Runs the released tooling against the emitted model, each command's
/// failure reported as `tooling-rejected`.
pub(crate) fn released_tooling(model: &Path) {
    let model = model.display().to_string();
    let commands: [(&str, Vec<&str>); 3] = [
        ("dcs-model", vec!["validate", model.as_str()]),
        ("dcs-model", vec!["lint", model.as_str()]),
        ("dcs-controller", vec![model.as_str(), "--check"]),
    ];
    let mut outputs = Vec::new();
    for (package, args) in commands {
        let output = released_tool(package, &args);
        assert!(
            output.status.success(),
            "{TOOLING_REJECTED}: cargo run -p {package} -- {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&output.stderr)
        );
        outputs.push(String::from_utf8(output.stdout).unwrap());
    }
    // The composed model is lint-clean: every point is signaled, every
    // signal carries unit/description/group, the writable point is an
    // internal setpoint, the field input declares its freshness budget,
    // and every channel is bound.
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
