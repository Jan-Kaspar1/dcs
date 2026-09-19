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
/// `Float`), reports `backup_active` (`Out`, `Bool`) while the
/// backup is selected, and — where the model wires it — reports
/// `backup_unhealthy` (`Out`, `Bool`) while the backup's own sample is
/// untrusted.
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
/// **`backup_unhealthy`.** The optional standby-health output —
/// declared only where the model wires it, `flow-paced-ratio`'s
/// `trim` convention. Where bound it asserts while the backup's own
/// sample is non-`Good` or non-finite — the same failed-measurement
/// predicate the selection rule applies to the primary — and reports
/// `false` while the backup reads `Good` and finite, independent of
/// which source is serving: a failed backup annunciates while the
/// primary still carries the measurement, so the loss of the unused
/// leg is visible before it is needed (QA finding
/// `backup-instrument-loss-silent-while-primary-healthy`, issue #502).
/// It always carries `Quality::Good`: it is the selector's own
/// computed state. An unwired instance selects identically and simply
/// does not expose the indication — documents emitted before the port
/// existed assemble unchanged.
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
    backup_unhealthy: Option<PointId>,
}

impl FailoverSelect {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "failover-select";

    /// Builds the component from explicit points. `backup_unhealthy`
    /// is `None` for an instance the model leaves unwired.
    pub fn new(
        name: impl Into<String>,
        primary: PointId,
        backup: PointId,
        out: PointId,
        backup_active: PointId,
        backup_unhealthy: Option<PointId>,
    ) -> Self {
        Self {
            name: name.into(),
            primary,
            backup,
            out,
            backup_active,
            backup_unhealthy,
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
        backup_unhealthy: Option<PointId>,
        _parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        Ok(Self::new(
            name,
            primary,
            backup,
            out,
            backup_active,
            backup_unhealthy,
        ))
    }
}

impl Component for FailoverSelect {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        let mut requirements = vec![
            IoRequirement::input::<f64>("primary", self.primary),
            IoRequirement::input::<f64>("backup", self.backup),
            IoRequirement::output::<f64>("out", self.out),
            IoRequirement::output::<bool>("backup_active", self.backup_active),
        ];
        // `backup_unhealthy` is the optional port: declared only where
        // the model wired one, so an unwired instance's descriptor and
        // I/O scope carry no `backup_unhealthy`.
        if let Some(backup_unhealthy) = self.backup_unhealthy {
            requirements.push(IoRequirement::output::<bool>(
                "backup_unhealthy",
                backup_unhealthy,
            ));
        }
        requirements
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
        // The standby-health report is independent of the selection: a
        // non-`Good` or non-finite backup asserts it whether the backup
        // is serving or idle, so a failed unused leg annunciates before
        // the primary's loss would select it.
        if let Some(backup_unhealthy) = self.backup_unhealthy {
            let backup_ok = backup.quality.is_good() && backup.value.is_finite();
            io.write_sample(
                backup_unhealthy,
                Sample::good(Value::Bool(!backup_ok), tick),
            )?;
        }
        Ok(())
    }

    /// Describes the selector: both sources are measured values, `out`
    /// the driven selection, `backup_active` the reported mode flag,
    /// `backup_unhealthy` the standby-health report joining the
    /// descriptor only where bound. No parameters.
    fn describe(&self) -> ComponentDescriptor {
        let mut roles: Vec<(&str, PortRole)> = vec![
            ("primary", PortRole::ProcessValue),
            ("backup", PortRole::ProcessValue),
            ("out", PortRole::Output),
            ("backup_active", PortRole::Status),
        ];
        if self.backup_unhealthy.is_some() {
            roles.push(("backup_unhealthy", PortRole::Status));
        }
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &roles,
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
    const UNHEALTHY: PointId = PointId(22);

    /// An instance without the optional standby-health output — the
    /// form documents emitted before the port existed take.
    fn component() -> FailoverSelect {
        FailoverSelect::new("fsel", PRIMARY, BACKUP, OUT, ACTIVE, None)
    }

    /// An instance with `backup_unhealthy` wired.
    fn wired_component() -> FailoverSelect {
        FailoverSelect::new("fsel", PRIMARY, BACKUP, OUT, ACTIVE, Some(UNHEALTHY))
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

    /// The wired variant's I/O — the extra `Out` point the optional
    /// port drives.
    fn wired_io() -> TestIo {
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
            (
                UNHEALTHY,
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

    fn unhealthy(io: &TestIo) -> bool {
        match io.written(UNHEALTHY).unwrap().value {
            Value::Bool(unhealthy) => unhealthy,
            value => panic!("backup_unhealthy must be Bool, got {value:?}"),
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
    fn a_bad_backup_annunciates_while_the_primary_serves() {
        // The issue-#502 reproduction: the backup fails while the
        // healthy primary keeps serving. The selection must not move,
        // `backup_active` must stay clear — and `backup_unhealthy`
        // must surface the standby's loss so the station can alarm it.
        for reason in [
            Quality::Bad(QualityReason::DeviceFault),
            Quality::Bad(QualityReason::CommunicationFault),
            Quality::Uncertain(QualityReason::Stale),
        ] {
            let mut block = wired_component();
            let io = wired_io();
            feed(&io, BACKUP, 9.0, reason);
            block.step(&io, Tick(1)).unwrap();
            assert_eq!(out(&io).value, Value::Float(1.0), "{reason:?}");
            assert_eq!(out(&io).quality, Quality::Good, "{reason:?}");
            assert!(!active(&io), "{reason:?}");
            assert!(unhealthy(&io), "{reason:?}");
            assert_eq!(
                io.written(UNHEALTHY).unwrap().quality,
                Quality::Good,
                "{reason:?}: the report is the selector's own computed state"
            );
        }
    }

    #[test]
    fn backup_unhealthy_reports_the_backup_independent_of_the_selection() {
        let mut block = wired_component();
        let io = wired_io();
        // A healthy backup reads clear while it serves.
        feed(&io, PRIMARY, 1.0, Quality::Bad(QualityReason::DeviceFault));
        block.step(&io, Tick(1)).unwrap();
        assert!(active(&io));
        assert!(!unhealthy(&io));
        // And a failed backup reports unhealthy while it is serving —
        // the two flags are not exclusive.
        feed(&io, BACKUP, 9.0, Quality::Bad(QualityReason::DeviceFault));
        block.step(&io, Tick(2)).unwrap();
        assert!(active(&io));
        assert!(unhealthy(&io));
        // A recovered backup clears the report the same scan its
        // sample turns Good again.
        feed(&io, BACKUP, 9.0, Quality::Good);
        block.step(&io, Tick(3)).unwrap();
        assert!(!unhealthy(&io));
        // A non-finite backup value counts as a failed measurement.
        feed(&io, BACKUP, f64::NAN, Quality::Good);
        block.step(&io, Tick(4)).unwrap();
        assert!(unhealthy(&io));
    }

    #[test]
    fn an_unwired_backup_unhealthy_selects_identically() {
        // The optional port's absence changes nothing the selection
        // computes — documents emitted before the port existed behave
        // unchanged.
        let mut block = component();
        let io = io();
        feed(&io, BACKUP, 9.0, Quality::Bad(QualityReason::DeviceFault));
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(out(&io).value, Value::Float(1.0));
        assert_eq!(out(&io).quality, Quality::Good);
        assert!(!active(&io));
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
            rationalization: None,
            ports: BTreeMap::new(),
        };
        FailoverSelect::from_parameters(
            "fsel",
            PRIMARY,
            BACKUP,
            OUT,
            ACTIVE,
            Some(UNHEALTHY),
            &instance.parameters,
        )
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

    #[test]
    fn the_wired_variant_describes_the_optional_port() {
        let ports = wired_component().describe().ports;
        let unhealthy = ports
            .iter()
            .find(|port| port.name == "backup_unhealthy")
            .expect("a wired instance describes backup_unhealthy");
        assert_eq!(unhealthy.direction, Direction::Out);
        assert_eq!(unhealthy.kind, ValueKind::Bool);
        assert_eq!(unhealthy.role, Some(PortRole::Status));
        assert_eq!(ports.len(), 5);
        // The unwired instance's descriptor carries no trace of it.
        assert!(
            component()
                .describe()
                .ports
                .iter()
                .all(|port| port.name != "backup_unhealthy"),
            "an unwired instance must not declare the optional port"
        );
    }
}
