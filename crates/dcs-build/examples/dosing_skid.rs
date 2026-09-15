//! Emits the reference dosing-skid document — the checked-in
//! `crates/dcs-demo/fixtures/dosing_skid.json` — on stdout:
//!
//! ```sh
//! cargo run -p dcs-build --example dosing_skid > crates/dcs-demo/fixtures/dosing_skid.json
//! ```

fn main() {
    let skid = dcs_build::dosing::dosing_skid(&dcs_build::dosing::DosingSkidConfig::reference())
        .expect("the reference dosing skid composes");
    println!("{}", serde_json::to_string_pretty(&skid.model).unwrap());
}
