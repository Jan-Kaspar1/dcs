//! Emits the connected station model or its field dynamics.
fn main() {
    let mut config = dcs_build::water_area::WaterAreaConfig::default();
    if std::env::args().any(|a| a == "--three-pumps") {
        config.station.pumps = 3;
    }
    let area = dcs_build::water_area::water_area(&config).expect("water area composes");
    if std::env::args().any(|a| a == "--dynamics") {
        println!("{}", serde_json::to_string_pretty(&area.dynamics).unwrap());
    } else {
        println!("{}", serde_json::to_string_pretty(&area.model).unwrap());
    }
}
