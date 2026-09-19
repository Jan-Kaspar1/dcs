//! The station's emit entry point — the one binary this repository
//! builds.
//!
//! - `pump-station` prints the emitted [`PlantModel`] document — the
//!   canonical serialization `ci/check.sh` byte-compares against the
//!   approved `model/plant.json`.
//! - `pump-station --fingerprint` prints the model's
//!   [`ModelFingerprint`] — the value `deploy/manifest.json` records.
//! - `pump-station --scenario` prints the scripted-simulation
//!   declaration `ci/simulate.py` runs — generated from the composed
//!   layout so the scenario can never drift from the point ids.
//!
//! Emission is deterministic: identical invocations emit identical
//! bytes, and the emitted document carries `version == MODEL_VERSION`.

mod scenario;
mod station;

use std::process::ExitCode;

const USAGE: &str = "\
usage: pump-station [--fingerprint | --scenario]

  (no flag)      emit the plant model document on stdout
  --fingerprint  print the model's canonical fingerprint
  --scenario     print the scripted-simulation declaration";

fn main() -> ExitCode {
    let station = match station::lift_station(&station::SiteConfig::declared()) {
        Ok(station) => station,
        Err(error) => {
            eprintln!("error: the station composition does not build: {error}");
            return ExitCode::FAILURE;
        }
    };
    assert_eq!(
        station.model.version,
        dcs_model::MODEL_VERSION,
        "the emitted document must carry the release's MODEL_VERSION"
    );
    match std::env::args().nth(1).as_deref() {
        None => println!("{}", serde_json::to_string_pretty(&station.model).unwrap()),
        Some("--fingerprint") => println!("{}", station.model.fingerprint()),
        Some("--scenario") => println!(
            "{}",
            serde_json::to_string_pretty(&scenario::scenario(&station.layout)).unwrap()
        ),
        Some("--help" | "-h") => println!("{USAGE}"),
        Some(other) => {
            eprintln!("error: unknown argument {other:?}\n{USAGE}");
            return ExitCode::FAILURE;
        }
    }
    ExitCode::SUCCESS
}
