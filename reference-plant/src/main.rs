//! The station's emit entry point — the one binary this repository
//! builds.
//!
//! - `pump-station` prints the emitted [`PlantModel`] document — the
//!   canonical serialization `ci/check.sh` byte-compares against the
//!   approved `model/plant.json`.
//! - `pump-station --revision-2` prints the station's in-service
//!   revision-2 document — the same composition plus the compatible
//!   added internal point the `revision-roll` clean-CI leg rolls
//!   through the pair's `--revised` standby path.
//! - `pump-station --fingerprint` prints the model's
//!   [`ModelFingerprint`] — the value `deploy/manifest.json` records.
//! - `pump-station --revision-2 --fingerprint` prints revision 2's
//!   fingerprint — the identity the roll asserts the revised peer serves.
//! - `pump-station --scenario` prints the scripted-simulation
//!   declaration `ci/simulate.py` runs — generated from the composed
//!   layout so the scenario can never drift from the point ids.
//!
//! Emission is deterministic: identical invocations emit identical
//! bytes, and the emitted document carries `version == MODEL_VERSION`.
//! The default emit is the approved `model/plant.json`; the revision-2
//! emit differs from it only in the compatible addition, so the check's
//! `dcs-model diff` names exactly that change.

mod scenario;
mod station;

use std::process::ExitCode;

const USAGE: &str = "\
usage: pump-station [--revision-2] [--fingerprint | --scenario]

  (no flag)      emit the plant model document on stdout
  --revision-2   emit the in-service revision-2 document instead —
                 combinable with --fingerprint
  --fingerprint  print the model's canonical fingerprint
  --scenario     print the scripted-simulation declaration";

fn main() -> ExitCode {
    let revised = std::env::args().any(|arg| arg == "--revision-2");
    let station = match if revised {
        station::lift_station_revision2(&station::SiteConfig::declared())
    } else {
        station::lift_station(&station::SiteConfig::declared())
    } {
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
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let flag = match argv
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .as_slice()
    {
        [] => None,
        ["--revision-2"] => None,
        [first] => Some(*first),
        ["--revision-2", second] => Some(*second),
        _ => Some("?"),
    };
    match flag {
        None => println!("{}", serde_json::to_string_pretty(&station.model).unwrap()),
        Some("--fingerprint") => println!("{}", station.model.fingerprint()),
        Some("--scenario") if !revised => println!(
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
