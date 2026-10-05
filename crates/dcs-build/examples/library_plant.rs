//! Emits the composed water/wastewater library plant's document — the
//! checked-in `crates/dcs-demo/fixtures/library_plant.json` — on
//! stdout:
//!
//! ```sh
//! cargo run -p dcs-build --example library_plant > crates/dcs-demo/fixtures/library_plant.json
//! ```

fn main() {
    let plant = dcs_build::library_plant::library_plant(
        &dcs_build::library_plant::LibraryPlantConfig::reference(),
    )
    .expect("the reference library plant composes");
    println!("{}", serde_json::to_string_pretty(&plant.model).unwrap());
}
