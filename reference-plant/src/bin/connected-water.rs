//! Customer-owned engineering of the connected area through released DCS APIs.
//! The optional binary is enabled after repinning to a release that contains
//! water_area; the existing v0.10.0 plant CI remains independently reproducible.
use dcs_build::water_area::{WaterAreaConfig, water_area};
fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut site = WaterAreaConfig {
        station: dcs_build::station::PumpStationConfig::reference(),
        dt: 0.2,        // seconds per scan / simulation step
        inflow: 36.0,   // m3/h
        pump_flow: 72.0,// m3/h per applied running-pump output
        tank_area: 0.2, // m2, intentionally fast training vessels
    };
    site.station.motor_fault_ticks = 15;
    // Site policy is code, separate from platform releases. Adding the third
    // pump also adds its diagnostics, managed alarms, panel and diagram node.
    if args.iter().any(|arg| arg == "--add-pump") { site.station.pumps = 3; }
    let area = water_area(&site).expect("customer water area validates");
    if args.iter().any(|arg| arg == "--dynamics") {
        println!("{}", serde_json::to_string_pretty(&area.dynamics).unwrap());
    } else if args.iter().any(|arg| arg == "--fingerprint") {
        println!("{}", area.model.fingerprint());
    } else { println!("{}", serde_json::to_string_pretty(&area.model).unwrap()); }
}
