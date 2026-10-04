//! Emits the reference aeration-train document — the checked-in
//! `crates/dcs-demo/fixtures/aeration_train.json` — on stdout:
//!
//! ```sh
//! cargo run -p dcs-build --example aeration_train > crates/dcs-demo/fixtures/aeration_train.json
//! ```

fn main() {
    let train =
        dcs_build::aeration::aeration_train(&dcs_build::aeration::AerationTrainConfig::reference())
            .expect("the reference aeration train composes");
    println!("{}", serde_json::to_string_pretty(&train.model).unwrap());
}
