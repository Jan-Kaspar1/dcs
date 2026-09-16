//! Emits the Wago rig's checked-in documents —
//! `crates/dcs-demo/fixtures/wago_rig.json` (the `ethercat` binding),
//! `wago_rig_sim.json` (the `sim` binding), and `wago_rig_cyclic.json`
//! (the `sim-cyclic` binding) — on stdout:
//!
//! ```sh
//! cargo run -p dcs-build --example wago_rig -- ethercat \
//!     > crates/dcs-demo/fixtures/wago_rig.json
//! cargo run -p dcs-build --example wago_rig -- sim \
//!     > crates/dcs-demo/fixtures/wago_rig_sim.json
//! cargo run -p dcs-build --example wago_rig -- cyclic \
//!     > crates/dcs-demo/fixtures/wago_rig_cyclic.json
//! ```

use dcs_build::wago::{RigBinding, wago_rig};

fn main() {
    let binding = match std::env::args().nth(1).as_deref() {
        None | Some("ethercat") => RigBinding::Ethercat,
        Some("sim") => RigBinding::Sim,
        Some("cyclic") => RigBinding::Cyclic,
        Some(other) => panic!("usage: wago_rig [ethercat|sim|cyclic], got {other:?}"),
    };
    let rig = wago_rig(binding).expect("the rig composes");
    println!("{}", serde_json::to_string_pretty(&rig.model).unwrap());
}
