//! CLI tests for the `dcs-controller` binary: deterministic `--ticks`
//! output and nonzero exits naming the failing element.

use std::process::Command;

const BINARY: &str = env!("CARGO_BIN_EXE_dcs-controller");
const TANK_LOOP: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-assembly/fixtures/tank_loop.json"
);
const UNKNOWN_KIND: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-assembly/fixtures/invalid/unknown_component_kind.json"
);

fn run(args: &[&str]) -> std::process::Output {
    Command::new(BINARY).args(args).output().unwrap()
}

#[test]
fn two_ticks_runs_produce_identical_snapshot_output() {
    let first = run(&[TANK_LOOP, "--ticks", "50"]);
    let second = run(&[TANK_LOOP, "--ticks", "50"]);

    assert!(first.status.success());
    assert!(second.status.success());
    assert_eq!(first.stdout, second.stdout);

    let stdout = String::from_utf8(first.stdout).unwrap();
    let snapshot: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    assert_eq!(snapshot["tick"], 50);
    assert_eq!(snapshot["points"].as_array().unwrap().len(), 5);
}

#[test]
fn assembly_error_exits_nonzero_and_names_the_element() {
    let output = run(&[UNKNOWN_KIND, "--ticks", "10"]);
    assert!(!output.status.success());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(
        stderr.contains("component 1") && stderr.contains("\"flux-capacitor\""),
        "{stderr}"
    );
}

#[test]
fn load_errors_exit_nonzero() {
    let output = run(&["does-not-exist.json", "--ticks", "10"]);
    assert!(!output.status.success());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains("does-not-exist.json"), "{stderr}");

    // A checked-in document that parses but fails model validation.
    const INVALID_MODEL: &str = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../dcs-model/fixtures/invalid/duplicate_id.json"
    );
    let output = run(&[INVALID_MODEL, "--ticks", "10"]);
    assert!(!output.status.success());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains("invalid plant model"), "{stderr}");
}

#[test]
fn missing_model_file_is_a_usage_error() {
    let output = run(&[]);
    assert_eq!(output.status.code(), Some(2));
}

#[test]
fn pair_token_requires_the_monitor_it_keys() {
    // The keyed line proofs live on the monitor's /checkpoint endpoint,
    // so the token means nothing without --listen.
    let output = run(&[TANK_LOOP, "--ticks", "5", "--pair-token", "secret"]);
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(
        stderr.contains("--pair-token requires --listen"),
        "{stderr}"
    );

    // --check only assembles the model; the monitor flags do not apply.
    let output = run(&[TANK_LOOP, "--check", "--pair-token", "secret"]);
    assert_eq!(output.status.code(), Some(2));
}

#[test]
fn aliased_persistence_paths_are_a_usage_error() {
    // Finding state-file-alias-clobbers-append-durable-files: an
    // aliased --state-file used to pass every validation, then the
    // checkpoint's write-then-rename orphaned the append writer's
    // descriptor and crash-looped the restart on a checkpoint
    // document the strict replay cannot read. The alias is a usage
    // error named at parse — for each pair.
    let dir = std::env::temp_dir().join(format!("dcs-cli-alias-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let shared = dir.join("shared");
    let shared = shared.to_str().unwrap();

    for (first, second) in [
        ("--state-file", "--history-file"),
        ("--state-file", "--journal-file"),
        ("--journal-file", "--history-file"),
    ] {
        let output = run(&[
            TANK_LOOP,
            "--scan-ms",
            "100",
            "--listen",
            "127.0.0.1:0",
            first,
            shared,
            second,
            shared,
        ]);
        assert_eq!(
            output.status.code(),
            Some(2),
            "{first} aliased with {second} must exit 2"
        );
        let stderr = String::from_utf8(output.stderr).unwrap();
        assert!(
            stderr.contains(first) && stderr.contains(second) && stderr.contains("distinct"),
            "{first} and {second}: {stderr}"
        );
    }

    // A `..` detour spelling the same file is the same file.
    let detour = dir.join("sub").join("..").join("shared");
    let output = run(&[
        TANK_LOOP,
        "--scan-ms",
        "100",
        "--listen",
        "127.0.0.1:0",
        "--state-file",
        detour.to_str().unwrap(),
        "--history-file",
        shared,
    ]);
    assert_eq!(output.status.code(), Some(2));

    // Distinct paths launch cleanly — a paced --ticks run that scans
    // and exits is the contract the alias refusal must not trip.
    let state = dir.join("state.json");
    let journal = dir.join("journal.jsonl");
    let history = dir.join("history.jsonl");
    let output = run(&[
        TANK_LOOP,
        "--scan-ms",
        "100",
        "--ticks",
        "3",
        "--listen",
        "127.0.0.1:0",
        "--state-file",
        state.to_str().unwrap(),
        "--journal-file",
        journal.to_str().unwrap(),
        "--history-file",
        history.to_str().unwrap(),
    ]);
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(
        output.status.success(),
        "distinct paths must launch and run: {stderr}"
    );
    // And each file holds its own format — the state sink's rename
    // landed on its own path, the append sinks' records on theirs.
    let head = std::fs::read_to_string(&history).unwrap();
    assert!(
        head.lines().next().unwrap().contains("run_boundary"),
        "the history file keeps append-format records: {head}"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn self_addressed_tracking_source_is_a_usage_error() {
    // Finding self-tracking-standby-never-refused: --standby aimed at
    // the instance's own --listen socket used to launch and park
    // standby/orphaned/aligned on its own checkpoint stream — an
    // apparent standby covering no peer — and armed with
    // --auto-promote it self-promoted at every budget, each apply
    // counting the heartbeat miss its own source_owns_field:false
    // document carries. The self-addressed source is a usage error
    // named at parse, under every spelling that reaches the own
    // socket, for both tracking flags.
    for (flag, listen, target) in [
        // The reported spelling: a wildcard listen, a loopback target.
        ("--standby", "0.0.0.0:18080", "127.0.0.1:18080"),
        // The identical spelling.
        ("--standby", "127.0.0.1:18081", "127.0.0.1:18081"),
        // A resolved-equal name.
        ("--standby", "127.0.0.1:18082", "localhost:18082"),
        // The demotion-tracking flag refuses the same way.
        ("--peer", "0.0.0.0:18083", "127.0.0.1:18083"),
        ("--peer", "127.0.0.1:18084", "localhost:18084"),
    ] {
        let output = run(&[
            TANK_LOOP,
            "--scan-ms",
            "100",
            "--listen",
            listen,
            flag,
            target,
        ]);
        assert_eq!(
            output.status.code(),
            Some(2),
            "{flag} {target} on --listen {listen} must exit 2"
        );
        let stderr = String::from_utf8(output.stderr).unwrap();
        assert!(
            stderr.contains(flag) && stderr.contains("--listen"),
            "{flag} {target} on --listen {listen}: {stderr}"
        );
    }

    // The supported same-host pair — the tracking peer one port over
    // — still launches: a paced --ticks standby run that scans and
    // exits is the contract the refusal must not shadow.
    let output = run(&[
        TANK_LOOP,
        "--scan-ms",
        "100",
        "--ticks",
        "3",
        "--listen",
        "127.0.0.1:0",
        "--standby",
        "127.0.0.1:18085",
    ]);
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(
        output.status.success(),
        "a distinct-port same-host standby must launch and run: {stderr}"
    );
}

#[test]
fn driven_requires_listen_and_excludes_pacing() {
    // --driven needs the monitor the requests arrive through.
    let output = run(&[TANK_LOOP, "--driven"]);
    assert_eq!(output.status.code(), Some(2));

    // It replaces pacing, so --ticks and --scan-ms do not apply.
    for pacing in ["--ticks", "--scan-ms"] {
        let output = run(&[
            TANK_LOOP,
            "--driven",
            "--listen",
            "127.0.0.1:0",
            pacing,
            "5",
        ]);
        assert_eq!(output.status.code(), Some(2));
    }
}
