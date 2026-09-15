//! Emits the Wago rig's checked-in documents —
//! `crates/dcs-demo/fixtures/wago_rig.json` (the `ethercat` binding)
//! and `wago_rig_sim.json` (the `sim` binding) — on stdout:
//!
//! ```sh
//! cargo run -p dcs-build --example wago_rig -- ethercat \
//!     > crates/dcs-demo/fixtures/wago_rig.json
//! cargo run -p dcs-build --example wago_rig -- sim \
//!     > crates/dcs-demo/fixtures/wago_rig_sim.json
//! ```

use dcs_build::wago::{RigBinding, wago_rig};

fn main() {
    let binding = match std::env::args().nth(1).as_deref() {
        None | Some("ethercat") => RigBinding::Ethercat,
        Some("sim") => RigBinding::Sim,
        Some(other) => panic!("usage: wago_rig [ethercat|sim], got {other:?}"),
    };
    let rig = wago_rig(binding).expect("the rig composes");
    println!("{}", serde_json::to_string_pretty(&rig.model).unwrap());
}
