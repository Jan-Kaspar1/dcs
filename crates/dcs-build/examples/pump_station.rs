//! Emits the reference pumping-station document — the checked-in
//! `crates/dcs-demo/fixtures/pump_station.json` — on stdout:
//!
//! ```sh
//! cargo run -p dcs-build --example pump_station > crates/dcs-demo/fixtures/pump_station.json
//! ```

fn main() {
    let station =
        dcs_build::station::pumping_station(&dcs_build::station::PumpStationConfig::reference())
            .expect("the reference station composes");
    println!("{}", serde_json::to_string_pretty(&station.model).unwrap());
}
