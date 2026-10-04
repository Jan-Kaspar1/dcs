//! Emits the reference filter-bank document — the checked-in
//! `crates/dcs-demo/fixtures/filter_bank.json` — on stdout:
//!
//! ```sh
//! cargo run -p dcs-build --example filter_bank > crates/dcs-demo/fixtures/filter_bank.json
//! ```

fn main() {
    let bank = dcs_build::filter_bank::filter_bank(&dcs_build::filter_bank::FilterBankConfig::reference())
        .expect("the reference filter bank composes");
    println!("{}", serde_json::to_string_pretty(&bank.model).unwrap());
}
