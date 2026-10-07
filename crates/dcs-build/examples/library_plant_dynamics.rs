//! Emits the composed water/wastewater library plant's merged dynamics
//! declaration — the checked-in
//! `crates/dcs-demo/fixtures/library_plant_dynamics.json` — on stdout:
//!
//! ```sh
//! cargo run -p dcs-build --example library_plant_dynamics > crates/dcs-demo/fixtures/library_plant_dynamics.json
//! ```

fn main() {
    println!(
        "{}",
        serde_json::to_string_pretty(&dcs_build::library_plant::library_plant_dynamics()).unwrap()
    );
}
