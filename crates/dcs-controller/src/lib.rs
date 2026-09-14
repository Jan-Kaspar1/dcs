//! The library half of `dcs-controller`: the component registry every
//! controller instance — active or standby — assembles the plant model
//! against.
//!
//! The binary itself holds only pacing, transport, and process
//! lifecycle; the registry lives here so the redundancy path — a
//! standby assembling an equivalent executor — and tests build the same
//! component set the controller deploys.

#![warn(missing_docs)]

use dcs_assembly::{BuildError, ComponentRegistry};
use dcs_blocks::{
    AlarmMonitor, AnalogInput, AnalogOutput, DigitalInput, DigitalOutput, Interlock, Motor,
    OverrideSelect, Pid, Valve,
};
use dcs_core::ValueKind;
use dcs_runtime::Component;

/// Boxes a built component or lifts its construction failure into
/// [`BuildError`].
fn boxed<C, E>(result: Result<C, E>) -> Result<Box<dyn Component>, BuildError>
where
    C: Component + 'static,
    E: std::error::Error + 'static,
{
    result
        .map(|component| Box::new(component) as Box<dyn Component>)
        .map_err(BuildError::other)
}

/// The `dcs-blocks` kinds this controller can instantiate — every kind
/// the component library ships, keyed by each type's `KIND` constant.
/// Analog kinds dispatch on the bound raw point's value kind: an `Int`
/// channel builds the `i64` variant, anything else the `f64` one (a
/// `Bool` raw point then fails wiring as a type mismatch, naming the
/// element).
pub fn registry() -> ComponentRegistry {
    ComponentRegistry::new()
        .with(AnalogInput::<f64>::KIND, |spec| {
            let raw = spec.require("raw")?;
            let out = spec.require("out")?;
            if spec.point_kind(raw) == Some(ValueKind::Int) {
                boxed(AnalogInput::<i64>::from_parameters(
                    spec.name.as_str(),
                    raw,
                    out,
                    spec.parameters,
                ))
            } else {
                boxed(AnalogInput::<f64>::from_parameters(
                    spec.name.as_str(),
                    raw,
                    out,
                    spec.parameters,
                ))
            }
        })
        .with(AnalogOutput::<f64>::KIND, |spec| {
            let eng = spec.require("eng")?;
            let raw = spec.require("raw")?;
            if spec.point_kind(raw) == Some(ValueKind::Int) {
                boxed(AnalogOutput::<i64>::from_parameters(
                    spec.name.as_str(),
                    eng,
                    raw,
                    spec.parameters,
                ))
            } else {
                boxed(AnalogOutput::<f64>::from_parameters(
                    spec.name.as_str(),
                    eng,
                    raw,
                    spec.parameters,
                ))
            }
        })
        .with(Pid::KIND, |spec| {
            boxed(Pid::from_parameters(
                spec.name.as_str(),
                spec.require("sp")?,
                spec.require("pv")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(DigitalInput::KIND, |spec| {
            boxed(DigitalInput::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(DigitalOutput::KIND, |spec| {
            boxed(DigitalOutput::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(AlarmMonitor::KIND, |spec| {
            boxed(AlarmMonitor::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("alarm")?,
                spec.parameters,
            ))
        })
        .with(Interlock::KIND, |spec| {
            // Trip inputs are declared `trip_1` … `trip_N`; order the
            // bound points by numeric suffix, not lexically.
            let mut trips: Vec<_> = spec
                .ports
                .iter()
                .filter_map(|(name, point)| {
                    name.strip_prefix("trip_")
                        .and_then(|suffix| suffix.parse::<usize>().ok())
                        .map(|index| (index, *point))
                })
                .collect();
            trips.sort_by_key(|(index, _)| *index);
            let trips: Vec<_> = trips.into_iter().map(|(_, point)| point).collect();
            boxed(Interlock::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("permissive")?,
                trips,
                spec.require("out")?,
                spec.require("tripped")?,
                spec.parameters,
            ))
        })
        .with(OverrideSelect::KIND, |spec| {
            boxed(OverrideSelect::from_parameters(
                spec.name.as_str(),
                spec.require("control")?,
                spec.require("operator")?,
                spec.require("select")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(Valve::KIND, |spec| {
            boxed(Valve::from_parameters(
                spec.name.as_str(),
                spec.require("cmd")?,
                spec.require("out")?,
                spec.require("fb")?,
                spec.require("discrepancy")?,
                spec.parameters,
            ))
        })
        .with(Motor::KIND, |spec| {
            boxed(Motor::from_parameters(
                spec.name.as_str(),
                spec.require("cmd")?,
                spec.require("out")?,
                spec.require("run")?,
                spec.require("fault")?,
                spec.parameters,
            ))
        })
}
