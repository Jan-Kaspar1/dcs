//! The library half of `dcs-controller`: the component registry every
//! controller instance — active or standby — assembles the plant model
//! against.
//!
//! The binary itself holds only pacing, transport, and process
//! lifecycle; the registry lives here so the redundancy path — a
//! standby assembling an equivalent executor — and tests build the same
//! component set the controller deploys.

#![warn(missing_docs)]

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, DriverRegistry, assemble, resolve_drivers,
};
use dcs_blocks::{
    AlarmMonitor, AnalogInput, AnalogOutput, BackwashCoordinator, BlowerGroup, BlowerIo,
    BlowerOutputs, BoolGate, BoolLatchingAlarm, CoordinatorOutputs, Counter, DeviationMonitor,
    DigitalInput, DigitalOutput, EdgeTrigger, FailoverSelect, FilterIo, FlowPacedRatio,
    GroupOutputs, HeaderCoordinator, HeaderOutputs, Interlock, LatchingAlarm, ManagedAlarmIo,
    ManagedBoolLatchingAlarm, ManagedLatchingAlarm, ManualStation, MedianVoter, Motor,
    OverrideSelect, PermissiveInputs, PhaseMonitor, PhaseMonitorIo, Pid, PumpGroup, PumpIo,
    RateLimiter, RatioOutputs, Sequencer, SignalFilter, SrLatch, ThresholdChain, ThresholdOutputs,
    Timer, Totalizer, Valve, ZoneIo,
};
use dcs_core::ValueKind;
use dcs_model::PlantModel;
use dcs_runtime::Component;
use std::collections::BTreeMap;
use std::fmt;

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
        .with(LatchingAlarm::KIND, |spec| {
            // Decision 70: the alarm's rationalization record is required
            // at construction — assembly is where kind and instance meet.
            spec.require_rationalization()?;
            boxed(LatchingAlarm::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("ack")?,
                spec.require("alarm")?,
                spec.require("unacknowledged")?,
                spec.parameters,
            ))
        })
        .with(BoolLatchingAlarm::KIND, |spec| {
            spec.require_rationalization()?;
            boxed(BoolLatchingAlarm::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("ack")?,
                spec.require("alarm")?,
                spec.require("unacknowledged")?,
                spec.parameters,
            ))
        })
        .with(ManagedLatchingAlarm::KIND, |spec| {
            spec.require_rationalization()?;
            // `shelve`, `oos`, and `suppress` are the optional managed
            // inputs — bound only where the model wires them
            // (`ComponentSpec::get`); an unbound port declares no
            // requirement and exposes no managed surface.
            boxed(ManagedLatchingAlarm::from_parameters(
                spec.name.as_str(),
                ManagedAlarmIo {
                    input: spec.require("in")?,
                    ack: spec.require("ack")?,
                    shelve: spec.get("shelve"),
                    oos: spec.get("oos"),
                    suppress: spec.get("suppress"),
                    alarm: spec.require("alarm")?,
                    unacknowledged: spec.require("unacknowledged")?,
                    shelved: spec.require("shelved")?,
                    suppressed: spec.require("suppressed")?,
                    out_of_service: spec.require("out_of_service")?,
                },
                spec.parameters,
            ))
        })
        .with(ManagedBoolLatchingAlarm::KIND, |spec| {
            spec.require_rationalization()?;
            boxed(ManagedBoolLatchingAlarm::from_parameters(
                spec.name.as_str(),
                ManagedAlarmIo {
                    input: spec.require("in")?,
                    ack: spec.require("ack")?,
                    shelve: spec.get("shelve"),
                    oos: spec.get("oos"),
                    suppress: spec.get("suppress"),
                    alarm: spec.require("alarm")?,
                    unacknowledged: spec.require("unacknowledged")?,
                    shelved: spec.require("shelved")?,
                    suppressed: spec.require("suppressed")?,
                    out_of_service: spec.require("out_of_service")?,
                },
                spec.parameters,
            ))
        })
        .with(Interlock::KIND, |spec| {
            // Trip inputs are declared `trip_1` … `trip_N`; the shared
            // indexed-family accessor binds them in numeric order.
            let trips = spec.indexed("trip_");
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
        .with(Timer::KIND, |spec| {
            boxed(Timer::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(Counter::KIND, |spec| {
            boxed(Counter::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("reset")?,
                spec.require("count")?,
                spec.require("done")?,
                spec.parameters,
            ))
        })
        .with(RateLimiter::KIND, |spec| {
            boxed(RateLimiter::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(ManualStation::KIND, |spec| {
            boxed(ManualStation::from_parameters(
                spec.name.as_str(),
                spec.require("control")?,
                spec.require("manual")?,
                spec.require("mode")?,
                spec.require("out")?,
                spec.require("manual_active")?,
                spec.parameters,
            ))
        })
        .with(SignalFilter::KIND, |spec| {
            boxed(SignalFilter::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(MedianVoter::KIND, |spec| {
            boxed(MedianVoter::from_parameters(
                spec.name.as_str(),
                spec.require("in_1")?,
                spec.require("in_2")?,
                spec.require("in_3")?,
                spec.require("out")?,
                spec.require("discrepancy")?,
                spec.parameters,
            ))
        })
        .with(Totalizer::KIND, |spec| {
            boxed(Totalizer::from_parameters(
                spec.name.as_str(),
                spec.require("rate")?,
                spec.require("reset")?,
                spec.require("total")?,
                spec.parameters,
            ))
        })
        .with(Sequencer::KIND, |spec| {
            boxed(Sequencer::from_parameters(
                spec.name.as_str(),
                spec.require("run")?,
                spec.require("reset")?,
                spec.require("out")?,
                spec.require("step")?,
                spec.require("done")?,
                spec.parameters,
            ))
        })
        .with(BoolGate::KIND, |spec| {
            // The input set is declared `in_1` … `in_N` following the
            // interlock's `trip_N` convention.
            let inputs = spec.indexed("in_");
            boxed(BoolGate::from_parameters(
                spec.name.as_str(),
                inputs,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(PumpGroup::KIND, |spec| {
            // The pumps are declared `cmd_1` … `cmd_N`, `run_1` …
            // `run_N`, `fault_1` … `fault_N`, `avail_1` … `avail_N`
            // following the interlock's `trip_N` convention; the shared
            // multi-family accessor counts and requires them.
            let pumps = spec
                .indexed_families(["cmd_", "run_", "fault_", "avail_"])?
                .into_iter()
                .map(|[cmd, run, fault, avail]| PumpIo {
                    cmd,
                    run,
                    fault,
                    avail,
                })
                .collect();
            boxed(PumpGroup::from_parameters(
                spec.name.as_str(),
                spec.require("demand")?,
                pumps,
                GroupOutputs {
                    duty: spec.require("duty")?,
                    staged: spec.require("staged")?,
                    none_available: spec.require("none_available")?,
                    all_faulted: spec.require("all_faulted")?,
                },
                spec.parameters,
            ))
        })
        .with(BlowerGroup::KIND, |spec| {
            // The blowers are declared `cmd_1` … `cmd_N`, `run_1` …
            // `run_N`, `fault_1` … `fault_N`, `avail_1` … `avail_N`,
            // `capacity_1` … `capacity_N`, `vent_1` … `vent_N`
            // following the pump-group convention; the shared
            // multi-family accessor counts and requires them.
            // `approve` is the optional operator release — bound only
            // where the model wires it; `staging_authority = 1`
            // without it fails construction naming the parameter.
            let blowers = spec
                .indexed_families(["cmd_", "run_", "fault_", "avail_", "capacity_", "vent_"])?
                .into_iter()
                .map(|[cmd, run, fault, avail, capacity, vent]| BlowerIo {
                    cmd,
                    run,
                    fault,
                    avail,
                    capacity,
                    vent,
                })
                .collect();
            boxed(BlowerGroup::from_parameters(
                spec.name.as_str(),
                spec.require("demand")?,
                spec.get("approve"),
                blowers,
                BlowerOutputs {
                    staged: spec.require("staged")?,
                    none_available: spec.require("none_available")?,
                    all_faulted: spec.require("all_faulted")?,
                    staging_pending: spec.require("staging_pending")?,
                    transition: spec.require("transition")?,
                },
                spec.parameters,
            ))
        })
        .with(SrLatch::KIND, |spec| {
            boxed(SrLatch::from_parameters(
                spec.name.as_str(),
                spec.require("set")?,
                spec.require("reset")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(EdgeTrigger::KIND, |spec| {
            boxed(EdgeTrigger::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(ThresholdChain::KIND, |spec| {
            boxed(ThresholdChain::from_parameters(
                spec.name.as_str(),
                spec.require("level")?,
                ThresholdOutputs {
                    demand: spec.require("demand")?,
                    duty_call: spec.require("duty_call")?,
                    lag_call: spec.require("lag_call")?,
                    below_cutoff: spec.require("below_cutoff")?,
                    high_level: spec.require("high_level")?,
                },
                spec.parameters,
            ))
        })
        .with(FailoverSelect::KIND, |spec| {
            boxed(FailoverSelect::from_parameters(
                spec.name.as_str(),
                spec.require("primary")?,
                spec.require("backup")?,
                spec.require("out")?,
                spec.require("backup_active")?,
                spec.parameters,
            ))
        })
        .with(FlowPacedRatio::KIND, |spec| {
            // `trim` is the optional analyzer correction — bound only
            // where the model wires it (`ComponentSpec::get`); an
            // unwired instance paces untrimmed.
            boxed(FlowPacedRatio::from_parameters(
                spec.name.as_str(),
                spec.require("flow")?,
                spec.require("dose")?,
                spec.get("trim"),
                RatioOutputs {
                    demand: spec.require("demand")?,
                    clamped: spec.require("clamped")?,
                    fallback_active: spec.require("fallback_active")?,
                },
                spec.parameters,
            ))
        })
        .with(DeviationMonitor::KIND, |spec| {
            boxed(DeviationMonitor::from_parameters(
                spec.name.as_str(),
                spec.require("expected")?,
                spec.require("measured")?,
                spec.require("deviation")?,
                spec.require("deviating")?,
                spec.parameters,
            ))
        })
        .with(BackwashCoordinator::KIND, |spec| {
            // The filters are declared `request_1` … `request_N`,
            // `grant_1` … `grant_N`, `position_1` … `position_N`
            // following the interlock's `trip_N` convention. The
            // filter count is the highest bound index across the
            // three families, and every index below it must bind all
            // three — a partial family or a gap fails `UnboundPort`
            // naming the missing member. `reorder` is the optional
            // operator instruction — bound only where the model wires
            // it (`ComponentSpec::get`); an unwired instance exposes
            // no reorder surface.
            let mut indices = std::collections::BTreeSet::new();
            for prefix in ["request_", "grant_", "position_"] {
                indices.extend(spec.ports.keys().filter_map(|name| {
                    name.strip_prefix(prefix)
                        .and_then(|suffix| suffix.parse::<usize>().ok())
                }));
            }
            let count = indices.iter().next_back().copied().unwrap_or(0);
            let mut filters = Vec::with_capacity(count);
            for index in 1..=count {
                filters.push(FilterIo {
                    request: spec.require(&format!("request_{index}"))?,
                    grant: spec.require(&format!("grant_{index}"))?,
                    position: spec.require(&format!("position_{index}"))?,
                });
            }
            boxed(BackwashCoordinator::from_parameters(
                spec.name.as_str(),
                PermissiveInputs {
                    supply_ok: spec.require("supply_ok")?,
                    waste_ok: spec.require("waste_ok")?,
                    flow_ok: spec.require("flow_ok")?,
                },
                spec.get("reorder"),
                filters,
                CoordinatorOutputs {
                    active: spec.require("active")?,
                    queued: spec.require("queued")?,
                    resource_blocked: spec.require("resource_blocked")?,
                },
                spec.parameters,
            ))
        })
        .with(HeaderCoordinator::KIND, |spec| {
            // The zones are declared `valve_pos_1` … `valve_pos_N`,
            // `airflow_1` … `airflow_N`, `pulsing_1` … `pulsing_N`,
            // `pulse_grant_1` … `pulse_grant_N` following the
            // interlock's `trip_N` convention; the shared multi-family
            // accessor counts and requires them — a partial family or
            // a gap fails `UnboundPort` naming the missing member.
            let zones = spec
                .indexed_families(["valve_pos_", "airflow_", "pulsing_", "pulse_grant_"])?
                .into_iter()
                .map(|[valve_pos, airflow, pulsing, pulse_grant]| ZoneIo {
                    valve_pos,
                    airflow,
                    pulsing,
                    pulse_grant,
                })
                .collect();
            boxed(HeaderCoordinator::from_parameters(
                spec.name.as_str(),
                spec.require("pressure")?,
                zones,
                HeaderOutputs {
                    pressure_sp: spec.require("pressure_sp")?,
                    blower_demand: spec.require("blower_demand")?,
                    most_open: spec.require("most_open")?,
                    at_bound: spec.require("at_bound")?,
                    pulse_blocked: spec.require("pulse_blocked")?,
                },
                spec.parameters,
            ))
        })
        .with(PhaseMonitor::KIND, |spec| {
            boxed(PhaseMonitor::from_parameters(
                spec.name.as_str(),
                PhaseMonitorIo {
                    input: spec.require("in")?,
                    phase: spec.require("phase")?,
                    capture: spec.require("capture")?,
                    deviation: spec.require("deviation")?,
                    exceeded: spec.require("exceeded")?,
                    overdue: spec.require("overdue")?,
                },
                spec.parameters,
            ))
        })
}

/// What a `--check` run reports: the assembly surface the model built —
/// devices resolved through the driver registry, points the assembled
/// executor serves, and components the component registry constructed —
/// counted so the output is deterministic across runs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CheckReport {
    /// Declared devices counted by `kind`, ordered by kind name.
    pub devices: BTreeMap<String, usize>,
    /// Declared `io_points`, field and internal alike.
    pub declared_points: usize,
    /// Points the assembled executor serves: the declared points plus
    /// the internal carriers port-to-port wiring synthesizes.
    pub served_points: usize,
    /// Constructed components counted by `kind`, ordered by kind name.
    pub components: BTreeMap<String, usize>,
}

impl fmt::Display for CheckReport {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let devices: usize = self.devices.values().sum();
        let components: usize = self.components.values().sum();
        writeln!(f, "devices: {devices}")?;
        for (kind, count) in &self.devices {
            writeln!(f, "  {kind}: {count}")?;
        }
        writeln!(
            f,
            "io_points: {} declared, {} served",
            self.declared_points, self.served_points
        )?;
        writeln!(f, "components: {components}")?;
        for (kind, count) in &self.components {
            writeln!(f, "  {kind}: {count}")?;
        }
        Ok(())
    }
}

/// The `--check` entry function: resolves the model's devices through
/// the standard [`DriverRegistry`], builds the fan-out driver, and
/// constructs every component through [`registry`] — the same driver
/// resolution and assembly a run performs — then reports what
/// assembled. No scan executes and no listener binds; every failure is
/// the [`AssemblyError`] a run would report, naming the offending model
/// element.
///
/// The caller loads and validates the document first — `check` takes a
/// `PlantModel`, which [`PlantModel::load`] yields only for a validated
/// model — so a load or validation failure reports before this runs,
/// exactly as it does in a run.
pub fn check(model: &PlantModel) -> Result<CheckReport, AssemblyError> {
    let driver = resolve_drivers(model, &DriverRegistry::standard())?.build()?;
    let executor = assemble(model, &registry(), &driver)?;
    let served_points = executor.snapshot().points.len();
    Ok(CheckReport {
        devices: count_by_kind(model.devices.iter().map(|device| device.kind.as_str())),
        declared_points: model.io_points.len(),
        served_points,
        components: count_by_kind(
            model
                .components
                .iter()
                .map(|instance| instance.kind.as_str()),
        ),
    })
}

/// Kind → instance count over `kinds`, ordered by kind name so the
/// rendered report is deterministic.
fn count_by_kind<'k>(kinds: impl Iterator<Item = &'k str>) -> BTreeMap<String, usize> {
    let mut counts = BTreeMap::new();
    for kind in kinds {
        *counts.entry(kind.to_string()).or_insert(0) += 1;
    }
    counts
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The registry-side pin of the spec-coverage guard: the standard
    /// registry registers exactly the kinds `dcs-blocks` ships — the
    /// checked-in [`dcs_blocks::KINDS`] list that
    /// `dcs-blocks/tests/spec_drift.rs` pins the `dcs-build` spec table
    /// against. A kind registered here without the list entry — or one
    /// dropped from here while still listed — fails this test.
    #[test]
    fn registry_registers_exactly_the_shipped_kinds() {
        let registry = registry();
        let registered: std::collections::BTreeSet<&str> = registry.kinds().collect();
        let shipped: std::collections::BTreeSet<&str> = dcs_blocks::KINDS.iter().copied().collect();
        assert_eq!(registered, shipped);
    }
}
