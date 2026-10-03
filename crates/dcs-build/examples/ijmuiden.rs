//! Emits the IJmuiden-pattern document — the checked-in
//! `crates/dcs-demo/fixtures/ijmuiden.json` — on stdout:
//!
//! ```sh
//! cargo run -p dcs-build --example ijmuiden > crates/dcs-demo/fixtures/ijmuiden.json
//! ```

fn main() {
    let scenario = dcs_build::ijmuiden::ijmuiden(&dcs_build::ijmuiden::IjmuidenConfig::reference())
        .expect("the reference IJmuiden scenario composes");
    println!("{}", serde_json::to_string_pretty(&scenario.model).unwrap());
}
