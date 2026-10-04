//! Canonical spellings for declared engineering units.
//!
//! A `unit` declaration — on an [`IoPoint`](dcs_model::IoPoint), a
//! [`Port`](dcs_model::Port), a [`ParamDecl`](crate::ParamDecl), or a
//! [`Signal`](dcs_model::Signal) — is a string the document compares
//! literally: two ends agree when their declared spellings are equal.
//! The constants here are the spellings the spec table and the
//! reference compositions use, so `"m3/h"` is written once and a
//! connection fails on a real difference rather than on a typo. The
//! set is a convention, not a closed vocabulary — a plant declares any
//! string its engineering data uses; the checks only require that two
//! wired ends spell the same unit the same way.
//!
//! [`DIMENSIONLESS`] is the declared "no unit" marker: distinct from
//! an absent declaration (which stays uncheckable on connections), an
//! empty string declares the value *is* dimensionless — a status flag
//! or a ratio — and a connection from it to an end declaring a real
//! unit is rejected.

/// The declared dimensionless marker: `""` spells "this value has no
/// unit" as a checked declaration, where an absent `unit` declares
/// nothing and stays uncheckable.
pub const DIMENSIONLESS: &str = "";
/// One controller scan — the unit every `*_ticks` interval parameter
/// and every scan-counted quantity declares. Decision 70's
/// `response_ticks` and the timing parameters of the spec table are
/// `Int` values expressed in ticks; [`ParamDecl::with_unit`](crate::ParamDecl::with_unit)
/// records it. Contextual time like `pid`'s `dt` — documented as "the
/// process's time units" — is deliberately not pinned here.
pub const TICKS: &str = "ticks";
/// A dimensionless ratio or fraction — the unit `deviation-monitor`'s
/// `deviation` and similar relative values declare.
pub const FRACTION: &str = "fraction";
/// Cubic metres per hour — a volumetric flow.
pub const M3_PER_H: &str = "m3/h";
/// Litres — a volume or tank level.
pub const L: &str = "L";
/// Litres per scan — a per-scan volumetric rate, as the dosing
/// dynamics document's net-draw and refill integrals.
pub const L_PER_SCAN: &str = "L/scan";
/// Grams per hour — a chemical mass rate, the dosing skid's demand
/// chain.
pub const G_PER_H: &str = "g/h";
/// Grams — a totalized chemical mass.
pub const G: &str = "g";
/// Milligrams per litre — a dose concentration.
pub const MG_PER_L: &str = "mg/L";
/// Metres — a level or length.
pub const M: &str = "m";
/// Nephelometric turbidity units — the effluent-turbidity scale the
/// declared 0.3 NTU per-filter and 1 NTU shutdown tiers are expressed
/// in.
pub const NTU: &str = "NTU";
/// Kilopascals — the aeration discharge-header pressure the
/// `header-coordinator`'s set-point and bounds are expressed in.
pub const KPA: &str = "kPa";
/// Standard-state cubic metres per hour — the aeration train's airflow
/// demand, measurement, and totalization unit.
pub const SM3_PER_H: &str = "sm3/h";
/// Standard-state cubic metres — a totalized airflow.
pub const SM3: &str = "sm3";
/// Metres per second — a velocity.
pub const M_PER_S: &str = "m/s";
/// Milliamps — an analog raw-range unit, as a 4–20 mA loop.
pub const MA: &str = "mA";
/// Percent — a proportional command or opening.
pub const PERCENT: &str = "%";
/// Pump stroke count — the metering-pump pulse total.
pub const STROKES: &str = "strokes";
/// Pump count — a staged-demand or running count.
pub const PUMPS: &str = "pumps";
/// Stage count — a sequencer's position.
pub const STAGES: &str = "stages";
