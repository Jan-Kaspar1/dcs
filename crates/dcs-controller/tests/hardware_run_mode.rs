//! The hardware-bound path through the `dcs-controller` binary: the
//! deployment's `--bus` logical-bus → segment bindings, the paced-only
//! run mode a hardware-bound model admits, the named startup failures,
//! and the monitoring identity/health surface a field run must expose.
//!
//! No rig, no NIC, and no privileges: the segment is a recorded run
//! replayed in place of the interface (`--bus ecat0=@capture.json`),
//! which is the deployment's own explicit choice — never a substitution
//! the controller makes on its own.

mod support;

use dcs_core::IoHealth;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::process::Command;

use support::{CONTROLLER, settle_sink_health, spawn_controller_paced};

/// The checked-in Wago rig model: an `ethercat` device on logical bus
/// `ecat0`, with the declared identity and process-image mapping the
/// rig manifest records.
const RIG: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/wago_rig.json"
);

/// The recorded coupler run the tests bind `ecat0` to: one station, two
/// input and two output bytes, matching the rig model's declared
/// identity (vendor 33, product 750354, revision 1) and its two-byte
/// process-data layout.
const CAPTURE: &str = r#"{
    "name": "wago-rig-controller-binding",
    "stations": [
        {"position": 0, "name": "WAGO 750-354", "vendor_id": 33,
         "product_id": 750354, "revision": 1, "input_bytes": 2, "output_bytes": 2}
    ],
    "cycles": [
        {"outcome": "complete", "inputs": "0000"},
        {"outcome": "complete", "inputs": "0000"},
        {"outcome": "complete", "inputs": "0000"},
        {"outcome": "late", "inputs": "0100"},
        {"outcome": "complete", "inputs": "0000"},
        {"outcome": "complete", "inputs": "0000"},
        {"outcome": "complete", "inputs": "0000"},
        {"outcome": "complete", "inputs": "0000"}
    ]
}"#;

/// A scratch directory unique to the test binary, so parallel test
/// targets never share a capture path.
fn scratch_dir() -> PathBuf {
    let dir = std::env::temp_dir().join(format!("dcs-bus-binding-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// A checked-in-shaped capture written to a scratch path under `name`.
fn scratch(name: &str) -> PathBuf {
    let path = scratch_dir().join(format!("{name}.json"));
    std::fs::write(&path, CAPTURE).unwrap();
    path
}

/// The `--bus` arm binding `ecat0` to the recorded run at `path` —
/// two argv elements, as the flag takes them.
fn recorded(path: &std::path::Path) -> Vec<String> {
    vec![
        "--bus".to_string(),
        format!("ecat0=@{path}", path = path.display()),
    ]
}

/// The same arm as borrowed `&str`s, for a direct `run(&[...])` call.
fn recorded_args(path: &std::path::Path) -> [String; 2] {
    let arm = recorded(path);
    [arm[0].clone(), arm[1].clone()]
}

fn run(args: &[&str]) -> std::process::Output {
    Command::new(CONTROLLER).args(args).output().unwrap()
}

fn stderr(output: &std::process::Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

#[test]
fn an_unbound_hardware_bus_is_a_named_startup_failure() {
    // The model declares `ecat0` and the deployment says nothing about
    // it: startup fails naming the bus, before any telemetry is served.
    let output = run(&[RIG, "--ticks", "2"]);
    assert_eq!(output.status.code(), Some(1));
    let message = stderr(&output);
    assert!(message.contains("ecat0"), "{message}");
    assert!(message.contains("--bus"), "{message}");
    assert!(
        output.stdout.is_empty(),
        "the failed startup served telemetry as healthy before failing: {}",
        String::from_utf8_lossy(&output.stdout)
    );
}

#[test]
fn a_binding_to_an_interface_this_host_lacks_is_a_named_startup_failure() {
    // The deployment names a NIC the host does not have — a wiring
    // mistake, not a bus to open later. The failure names the bus and
    // the interface, and never falls back to a simulated bus.
    let output = run(&[RIG, "--ticks", "2", "--bus", "ecat0=dcs0-not-a-real-nic"]);
    assert_eq!(output.status.code(), Some(1));
    let message = stderr(&output);
    assert!(message.contains("ecat0"), "{message}");
    assert!(message.contains("dcs0-not-a-real-nic"), "{message}");
    assert!(message.contains("interface"), "{message}");
}

#[test]
fn a_binding_to_an_unreadable_recording_is_a_named_startup_failure() {
    let missing = scratch_dir().join("does-not-exist.json");
    let _ = std::fs::remove_file(&missing);
    let arm = recorded_args(&missing);
    let output = run(&[RIG, "--ticks", "2", &arm[0], &arm[1]]);
    assert_eq!(output.status.code(), Some(1));
    let message = stderr(&output);
    assert!(message.contains("ecat0"), "{message}");
    assert!(message.contains("recorded run"), "{message}");

    // A file that is not a capture at all names why it could not be
    // replayed, still under the bus it was bound to.
    let broken = scratch("broken");
    std::fs::write(&broken, "{\"name\": \"x\"}").unwrap();
    let arm = recorded_args(&broken);
    let output = run(&[RIG, "--ticks", "2", &arm[0], &arm[1]]);
    assert_eq!(output.status.code(), Some(1));
    let message = stderr(&output);
    assert!(message.contains("ecat0"), "{message}");
    assert!(message.contains("recorded run"), "{message}");
}

#[test]
fn a_binding_whose_recording_does_not_match_the_declared_identity_fails_before_serving() {
    // The recording is the segment, so a recording whose station
    // identity differs from the model's declaration is refused before
    // any exchange: the run never serves telemetry as healthy over a
    // segment it could not verify.
    // Same coupler, a different reported revision.
    let capture = scratch("wrong-identity");
    std::fs::write(
        &capture,
        CAPTURE.replace("\"revision\": 1", "\"revision\": 9"),
    )
    .unwrap();
    let arm = recorded_args(&capture);
    let output = run(&[RIG, "--ticks", "2", &arm[0], &arm[1]]);
    assert_eq!(output.status.code(), Some(1));
    let message = stderr(&output);
    assert!(message.contains("ecat0"), "{message}");
    assert!(message.contains("revision"), "{message}");
}

#[test]
fn check_assembles_a_hardware_model_without_binding_a_segment() {
    // `--check` is the engineering compile-check: it resolves the
    // device kinds and constructs the components without opening a
    // segment, so a hardware-bound model is checkable before any
    // deployment binding exists.
    let output = run(&[RIG, "--check"]);
    assert!(output.status.success(), "check failed: {}", stderr(&output));
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(stdout.contains("ethercat"), "{stdout}");
}

#[test]
fn check_rejects_a_bus_binding_as_a_run_mode_option() {
    // A binding is a run-mode arm: check mode binds nothing, so
    // accepting the flag there would silently ignore it.
    let output = run(&[RIG, "--check", "--bus", "ecat0=eth0"]);
    assert_eq!(output.status.code(), Some(2));
    assert!(stderr(&output).contains("--bus"), "{}", stderr(&output));
}

#[test]
fn a_hardware_model_refuses_the_driven_run_mode_by_name() {
    // The paced controller owns scans against hardware: the driven
    // schedule — scans arriving one at a time through `POST /scan` —
    // has no meaning on a real bus, so the conflict is refused rather
    // than resolved by substituting a simulation.
    let capture = scratch("driven-refusal");
    let arm = recorded_args(&capture);
    let output = run(&[
        RIG,
        "--listen",
        "127.0.0.1:0",
        "--driven",
        "--dt",
        "0.1",
        &arm[0],
        &arm[1],
    ]);
    assert_eq!(output.status.code(), Some(1));
    let message = stderr(&output);
    assert!(message.contains("--driven"), "{message}");
    assert!(message.contains("--scan-ms"), "{message}");
    assert!(message.contains("ecat0"), "{message}");
}

#[test]
fn the_paced_hardware_run_serves_the_identity_and_health_surface() {
    // The monitoring surface a field run must expose: the build
    // identity and model fingerprint on `/health`, and the per-bus
    // operational state, exchange freshness, communication failures,
    // and missed-deadline counters on the snapshot's I/O-health
    // section — through the decision-22 diagnostics surface, not a
    // separate hardware-only endpoint.
    let capture = scratch("identity");
    let controller = spawn_controller_paced(Path::new(RIG), &recorded(&capture), 20, "127.0.0.1:0");
    let client = dcs_monitor::MonitorClient::new(controller.addr);
    let mut health = None;
    for _ in 0..200 {
        if let Ok(report) = client.health() {
            health = Some(report);
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    let health = health.expect("the paced hardware run served /health");

    // Build identity: the crate version and the model fingerprint the
    // served model carries. The git revision is absent unless the build
    // recorded one — reported absent rather than invented.
    let build = health.build.expect("the run reports its build identity");
    assert_eq!(build.version, env!("CARGO_PKG_VERSION"));
    assert!(build.git_sha.as_deref().is_none_or(|sha| !sha.is_empty()));
    let fingerprint = build
        .model_fingerprint
        .expect("the run reports the model it loaded");
    assert!(!fingerprint.is_empty());

    // The served document carries the same identity a consumer decodes.
    let document = client.health().unwrap();
    let json = serde_json::to_value(&document).unwrap();
    assert_eq!(
        json["build"]["model_fingerprint"],
        Value::String(fingerprint.clone())
    );
    assert_eq!(json["build"]["version"], env!("CARGO_PKG_VERSION"));

    // The per-bus surface: the logical bus name, the deployment
    // binding that served it, its operational state on the field bus's
    // own vocabulary, its exchange freshness, and its counters.
    let mut snapshot = None;
    for _ in 0..400 {
        if let Ok(served) = client.snapshot()
            && served.tick.0 >= 5
        {
            snapshot = Some(served);
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    let mut snapshot = snapshot.expect("the paced hardware run served a snapshot");
    settle_sink_health(&mut snapshot);
    let io_health: &IoHealth = &snapshot.io_health;
    let driver = io_health
        .driver
        .as_ref()
        .expect("the hardware backend volunteers its transport diagnostics");
    assert_eq!(driver.link, dcs_core::LinkState::Connected);
    let exchange = driver
        .exchange
        .as_ref()
        .expect("the cyclic surface reports");
    assert!(exchange.attempted >= 5, "{exchange:?}");
    assert!(exchange.succeeded >= 4, "{exchange:?}");
    let bus = exchange
        .buses
        .first()
        .expect("the per-bus row the hardware backend volunteers");
    assert_eq!(bus.bus.as_deref(), Some("ecat0"));
    assert_eq!(bus.state, Some(dcs_core::OperationalState::Init));
    assert!(
        bus.binding
            .as_deref()
            .is_some_and(|binding| binding.starts_with('@')),
        "{bus:?}"
    );
    // The recording's fourth cycle is a late frame: it completed, and
    // its missed deadline is the counter the field surface must carry.
    assert!(
        bus.missed_deadlines >= 1,
        "the recorded late frame counts a missed deadline: {bus:?}"
    );
    assert_eq!(
        bus.last_exchange_tick, exchange.last_exchange_tick,
        "the per-bus freshness matches the aggregate's"
    );

    // Manual stepping stays refused under pacing — the wall clock owns
    // the schedule, which is the leg the hardware run relies on. The
    // client's own request names the refusal, so the assertion is on
    // the message the operator would read.
    let refused = client
        .advance(1)
        .expect_err("POST /scan must stay refused under pacing");
    assert!(refused.to_string().contains("paced"), "{refused}");
}
