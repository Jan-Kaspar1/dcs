//! Failover select: quality-driven selection between a primary and a
//! backup analog measurement — the degraded-measurement half of the
//! station level-control contract architecture decision 42 records.

use crate::describe;
use crate::params::{ParameterError, Parameters};
use dcs_core::{
    ComponentDescriptor, PointId, PortRole, Quality, QualityReason, Sample, Tick, Value,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// A two-source measurement failover: reads `primary` and `backup`
/// (`In`, `Float`), serves the selected sample on `out` (`Out`,
/// `Float`), and reports `backup_active` (`Out`, `Bool`) while the
/// backup is selected.
///
/// **Selection.** `out` carries `primary`'s sample while the primary
/// reads `Good` with a finite value — a non-finite reading cannot be
/// controlled on and counts as a failed measurement, the crate's
/// `NaN`-as-device-fault convention applied at the selector. Otherwise
/// `out` carries `backup`'s sample verbatim, quality included: a
/// `Bad` or `Uncertain` backup flows through, and when every source is
/// failed `out` is non-`Good` — a downstream `threshold-chain`'s
/// `on_bad_demand` rule then answers the all-measurement-bad case, the
/// interaction decision 42 records. A non-finite selected value —
/// possible only from the backup — merges `Bad(DeviceFault)` onto the
/// served quality, matching `override-select`.
///
/// `backup_active` asserts while the backup is selected — the
/// transition into backup mode is the alarmed condition the decision
/// calls for; the station fixture wires it into a
/// `bool-latching-alarm` whose `unacknowledged` latches the engagement
/// until the operator acknowledges — and reports `false` while the
/// primary serves. It always carries `Quality::Good`: it is the
/// selector's own computed state.
///
/// The selector is stateless and declares no parameters; a failed
/// primary's recovery re-selects it the same scan its sample turns
/// `Good` again — no latch, no soak timer.
#[derive(Debug)]
pub struct FailoverSelect {
    name: String,
    primary: PointId,
    backup: PointId,
    out: PointId,
    backup_active: PointId,
}

impl FailoverSelect {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "failover-select";

    /// Builds the component from explicit points.
    pub fn new(
        name: impl Into<String>,
        primary: PointId,
        backup: PointId,
        out: PointId,
        backup_active: PointId,
    ) -> Self {
        Self {
            name: name.into(),
            primary,
            backup,
            out,
            backup_active,
        }
    }

    /// Builds the component from a plant-model parameter map; the kind
    /// declares none, so the map is accepted unread.
    pub fn from_parameters(
        name: impl Into<String>,
        primary: PointId,
        backup: PointId,
        out: PointId,
        backup_active: PointId,
        _parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        Ok(Self::new(name, primary, backup, out, backup_active))
    }
}

impl Component for FailoverSelect {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("primary", self.primary),
            IoRequirement::input::<f64>("backup", self.backup),
            IoRequirement::output::<f64>("out", self.out),
            IoRequirement::output::<bool>("backup_active", self.backup_active),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let primary = io.read_typed::<f64>(self.primary)?;
        let backup = io.read_typed::<f64>(self.backup)?;
        let on_backup = !(primary.quality.is_good() && primary.value.is_finite());
        let selected = if on_backup { backup } else { primary };
        let mut quality = selected.quality;
        if !selected.value.is_finite() {
            quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
        }
        io.write_sample(
            self.out,
            Sample::new(Value::Float(selected.value), quality, tick),
        )?;
        io.write_sample(
            self.backup_active,
            Sample::good(Value::Bool(on_backup), tick),
        )?;
        Ok(())
    }

    /// Describes the selector: both sources are measured values, `out`
    /// the driven selection, `backup_active` the reported mode flag.
    /// No parameters.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[
                ("primary", PortRole::ProcessValue),
                ("backup", PortRole::ProcessValue),
                ("out", PortRole::Output),
                ("backup_active", PortRole::Status),
            ],
            vec![],
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, PortDescriptor, StateError, StateMap, ValueKind};
    use dcs_model::{ComponentId, ComponentInstance};
    use std::collections::BTreeMap;

    const PRIMARY: PointId = PointId(10);
    const BACKUP: PointId = PointId(11);
    const OUT: PointId = PointId(20);
    const ACTIVE: PointId = PointId(21);

    fn component() -> FailoverSelect {
        FailoverSelect::new("fsel", PRIMARY, BACKUP, OUT, ACTIVE)
    }

    fn io() -> TestIo {
        TestIo::new(&[
            (
                PRIMARY,
                Direction::In,
                Sample::good(Value::Float(1.0), Tick::ZERO),
            ),
            (
                BACKUP,
                Direction::In,
                Sample::good(Value::Float(9.0), Tick::ZERO),
            ),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                ACTIVE,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    fn feed(io: &TestIo, point: PointId, value: f64, quality: Quality) {
        io.feed(point, Sample::new(Value::Float(value), quality, Tick::ZERO));
    }

    fn out(io: &TestIo) -> Sample {
        io.written(OUT).unwrap()
    }

    fn active(io: &TestIo) -> bool {
        match io.written(ACTIVE).unwrap().value {
            Value::Bool(active) => active,
            value => panic!("backup_active must be Bool, got {value:?}"),
        }
    }

    #[test]
    fn the_good_primary_serves() {
        let mut block = component();
        let io = io();
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(out(&io).value, Value::Float(1.0));
        assert_eq!(out(&io).quality, Quality::Good);
        assert!(!active(&io));
    }

    #[test]
    fn a_non_good_primary_selects_the_backup() {
        for reason in [
            Quality::Bad(QualityReason::DeviceFault),
            Quality::Bad(QualityReason::CommunicationFault),
            Quality::Bad(QualityReason::ConfigurationFault),
            Quality::Uncertain(QualityReason::Stale),
            Quality::Uncertain(QualityReason::Substituted),
            Quality::Uncertain(QualityReason::OutOfRange),
        ] {
            let mut block = component();
            let io = io();
            feed(&io, PRIMARY, 1.0, reason);
            block.step(&io, Tick(1)).unwrap();
            assert_eq!(out(&io).value, Value::Float(9.0));
            assert_eq!(out(&io).quality, Quality::Good);
            assert!(active(&io));
        }
    }

    #[test]
    fn the_backup_serves_with_its_own_quality() {
        let mut block = component();
        let io = io();
        feed(&io, PRIMARY, 1.0, Quality::Bad(QualityReason::DeviceFault));
        feed(&io, BACKUP, 9.0, Quality::Uncertain(QualityReason::Stale));
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(out(&io).value, Value::Float(9.0));
        assert_eq!(out(&io).quality, Quality::Uncertain(QualityReason::Stale));
        assert!(active(&io));
    }

    #[test]
    fn when_every_source_is_bad_out_is_bad() {
        let mut block = component();
        let io = io();
        feed(&io, PRIMARY, 1.0, Quality::Bad(QualityReason::DeviceFault));
        feed(
            &io,
            BACKUP,
            9.0,
            Quality::Bad(QualityReason::CommunicationFault),
        );
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(
            out(&io).quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
        assert!(active(&io));
    }

    #[test]
    fn a_non_finite_primary_counts_as_failed() {
        let mut block = component();
        let io = io();
        feed(&io, PRIMARY, f64::NAN, Quality::Good);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(out(&io).value, Value::Float(9.0));
        assert!(active(&io));

        // A non-finite *backup* serves its value flagged as a device
        // fault — NaN cannot be controlled on.
        feed(&io, BACKUP, f64::NAN, Quality::Good);
        block.step(&io, Tick(2)).unwrap();
        assert!(matches!(out(&io).value, Value::Float(v) if v.is_nan()));
        assert_eq!(out(&io).quality, Quality::Bad(QualityReason::DeviceFault));
    }

    #[test]
    fn a_recovered_primary_reselects_without_a_latch() {
        let mut block = component();
        let io = io();
        feed(&io, PRIMARY, 1.0, Quality::Bad(QualityReason::DeviceFault));
        block.step(&io, Tick(1)).unwrap();
        assert!(active(&io));
        feed(&io, PRIMARY, 1.0, Quality::Good);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(out(&io).value, Value::Float(1.0));
        assert!(!active(&io));
    }

    #[test]
    fn backup_active_always_reports_good_quality() {
        let mut block = component();
        let io = io();
        feed(&io, PRIMARY, 1.0, Quality::Bad(QualityReason::DeviceFault));
        feed(&io, BACKUP, 9.0, Quality::Bad(QualityReason::DeviceFault));
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(io.written(ACTIVE).unwrap().quality, Quality::Good);
    }

    #[test]
    fn the_selector_is_stateless_across_capture_and_restore() {
        // The mode is derived from the inputs each scan — nothing is
        // captured, so a restored standby recomputes the same
        // selection from the same samples and a checkpoint mid-backup
        // needs no kind state to carry it.
        let mut block = component();
        let state = block.capture_state();
        assert!(state.is_empty());
        block.restore_state(&state).unwrap();

        // A field the kind never captured is rejected, not ignored.
        let mut incompatible = StateMap::new();
        incompatible.insert("on_backup", Value::Bool(true));
        assert!(matches!(
            block.restore_state(&incompatible),
            Err(StateError::UnknownField { ref field, .. }) if field == "on_backup"
        ));
    }

    #[test]
    fn builds_from_parameter_map() {
        // The kind declares no parameters: the empty map builds, and a
        // stray key is unread here — undeclared keys are the spec's
        // `UnknownParameter` case at composition time.
        let instance = ComponentInstance {
            id: ComponentId(1),
            kind: FailoverSelect::KIND.to_string(),
            parameters: Parameters::new(),
            ports: BTreeMap::new(),
        };
        FailoverSelect::from_parameters("fsel", PRIMARY, BACKUP, OUT, ACTIVE, &instance.parameters)
            .unwrap();
    }

    #[test]
    fn identical_inputs_produce_identical_outputs() {
        let run = || {
            let mut block = component();
            let io = io();
            let mut trace = Vec::new();
            for (tick, quality) in [
                Quality::Good,
                Quality::Bad(QualityReason::DeviceFault),
                Quality::Good,
            ]
            .into_iter()
            .enumerate()
            {
                feed(&io, PRIMARY, tick as f64, quality);
                block.step(&io, Tick(tick as u64 + 1)).unwrap();
                trace.push((out(&io), io.written(ACTIVE).unwrap()));
            }
            trace
        };
        assert_eq!(run(), run());
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "fsel");
        assert_eq!(descriptor.kind, FailoverSelect::KIND);
        assert_eq!(descriptor.label, "fsel");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "primary".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "backup".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "out".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Output),
                    point: None,
                },
                PortDescriptor {
                    name: "backup_active".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
            ]
        );
        assert!(descriptor.parameters.is_empty());
    }
}
