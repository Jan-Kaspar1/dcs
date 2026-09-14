//! Override selector: chooses between a control value and an operator
//! value on a select input, propagating worst-of input quality.

use crate::describe;
use crate::params::{ParameterError, Parameters};
use dcs_core::{
    ComponentDescriptor, PointId, PortRole, Quality, QualityReason, Sample, Tick, Value,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// An override selector: writes either the `control` or the `operator`
/// analog input to `out`, chosen by the boolean `select` input.
///
/// `select` `false` passes the `control` (automatic) value; `select`
/// `true` passes the `operator` (manual override) value. The choice is a
/// pure function of the current inputs — the selector holds no state, so
/// the source follows `select` on the scan it changes and switching is
/// deterministic.
///
/// `out`'s quality is the worst of all three inputs' qualities — control,
/// operator, and select merged — so a degraded source marks the output
/// even while it is the unselected one, and an untrusted `select` marks
/// the output whose source it chose. A `NaN` on the selected input
/// additionally merges `Bad(DeviceFault)`, matching the crate's
/// non-finite-input convention.
///
/// Declared I/O: `control` (`In`, `Float`), `operator` (`In`, `Float`),
/// `select` (`In`, `Bool`), `out` (`Out`, `Float`).
///
/// Parameters: none — `from_parameters` accepts the parameter map for
/// symmetry with the model-driven registry's other constructors.
#[derive(Debug)]
pub struct OverrideSelect {
    name: String,
    control: PointId,
    operator: PointId,
    select: PointId,
    output: PointId,
}

impl OverrideSelect {
    /// The component-kind string the model-driven registry maps onto this
    /// type's constructor.
    pub const KIND: &'static str = "override-select";

    /// Builds the component from explicit points.
    pub fn new(
        name: impl Into<String>,
        control: PointId,
        operator: PointId,
        select: PointId,
        output: PointId,
    ) -> Self {
        Self {
            name: name.into(),
            control,
            operator,
            select,
            output,
        }
    }

    /// Builds the component from a plant-model parameter map. This kind
    /// reads no parameters; the map is accepted so the registry can
    /// construct it like its siblings.
    pub fn from_parameters(
        name: impl Into<String>,
        control: PointId,
        operator: PointId,
        select: PointId,
        output: PointId,
        _parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        Ok(Self::new(name, control, operator, select, output))
    }
}

impl Component for OverrideSelect {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("control", self.control),
            IoRequirement::input::<f64>("operator", self.operator),
            IoRequirement::input::<bool>("select", self.select),
            IoRequirement::output::<f64>("out", self.output),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let control = io.read_typed::<f64>(self.control)?;
        let operator = io.read_typed::<f64>(self.operator)?;
        let select = io.read_typed::<bool>(self.select)?;
        let selected = if select.value { operator } else { control };
        let mut quality = control
            .quality
            .merge(operator.quality)
            .merge(select.quality);
        if selected.value.is_nan() {
            quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
        }
        io.write_sample(
            self.output,
            Sample::new(Value::Float(selected.value), quality, tick),
        )?;
        Ok(())
    }

    /// Describes the selector: `control` is the process-side value,
    /// `operator` the operator's target, `select` the reported mode
    /// choosing between them, `out` the driven value. The kind reads no
    /// parameters.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[
                ("control", PortRole::ProcessValue),
                ("operator", PortRole::Setpoint),
                ("select", PortRole::Status),
                ("out", PortRole::Output),
            ],
            Vec::new(),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, PortDescriptor, Quality, ValueKind};
    use dcs_model::{ComponentId, ComponentInstance};
    use std::collections::BTreeMap;

    const CONTROL: PointId = PointId(80);
    const OPERATOR: PointId = PointId(81);
    const SELECT: PointId = PointId(82);
    const OUT: PointId = PointId(83);

    fn component() -> OverrideSelect {
        OverrideSelect::new("ovr", CONTROL, OPERATOR, SELECT, OUT)
    }

    fn io(select: bool) -> TestIo {
        TestIo::new(&[
            (
                CONTROL,
                Direction::In,
                Sample::good(Value::Float(10.0), Tick::ZERO),
            ),
            (
                OPERATOR,
                Direction::In,
                Sample::good(Value::Float(20.0), Tick::ZERO),
            ),
            (
                SELECT,
                Direction::In,
                Sample::good(Value::Bool(select), Tick::ZERO),
            ),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
        ])
    }

    fn output(io: &TestIo) -> Sample {
        io.written(OUT).unwrap()
    }

    #[test]
    fn select_switches_between_control_and_operator() {
        let mut block = component();
        let io = io(false);

        // select=false passes the control value.
        block.step(&io, Tick(1)).unwrap();
        let out = output(&io);
        assert_eq!(out.value, Value::Float(10.0));
        assert_eq!(out.quality, Quality::Good);
        assert_eq!(out.tick, Tick(1));

        // select=true passes the operator value.
        io.feed(SELECT, Sample::good(Value::Bool(true), Tick(2)));
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(output(&io).value, Value::Float(20.0));

        // And back — the switch follows select on the scan it changes.
        io.feed(SELECT, Sample::good(Value::Bool(false), Tick(3)));
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(output(&io).value, Value::Float(10.0));
    }

    #[test]
    fn reports_worst_of_input_quality() {
        let mut block = component();

        // A Bad control input marks the output even while operator is
        // the selected source.
        let io = io(true);
        io.feed(
            CONTROL,
            Sample::new(
                Value::Float(10.0),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(1),
            ),
        );
        block.step(&io, Tick(1)).unwrap();
        let out = output(&io);
        assert_eq!(out.value, Value::Float(20.0));
        assert_eq!(out.quality, Quality::Bad(QualityReason::DeviceFault));

        // An Uncertain unselected input still degrades the output.
        io.feed(CONTROL, Sample::good(Value::Float(10.0), Tick(2)));
        io.feed(SELECT, Sample::good(Value::Bool(false), Tick(2)));
        io.feed(
            OPERATOR,
            Sample::new(
                Value::Float(20.0),
                Quality::Uncertain(QualityReason::Stale),
                Tick(2),
            ),
        );
        block.step(&io, Tick(2)).unwrap();
        let out = output(&io);
        assert_eq!(out.value, Value::Float(10.0));
        assert_eq!(out.quality, Quality::Uncertain(QualityReason::Stale));

        // A Bad select marks the output whose source it chose.
        io.feed(OPERATOR, Sample::good(Value::Float(20.0), Tick(3)));
        io.feed(
            SELECT,
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(3),
            ),
        );
        block.step(&io, Tick(3)).unwrap();
        let out = output(&io);
        assert_eq!(out.value, Value::Float(20.0));
        assert_eq!(out.quality, Quality::Bad(QualityReason::CommunicationFault));
    }

    #[test]
    fn nan_selected_value_marks_output_bad() {
        let mut block = component();
        let io = io(true);
        io.feed(OPERATOR, Sample::good(Value::Float(f64::NAN), Tick(1)));
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(
            output(&io).quality,
            Quality::Bad(QualityReason::DeviceFault)
        );

        // A NaN on the unselected input leaves the output's quality to
        // the inputs' own qualities.
        io.feed(SELECT, Sample::good(Value::Bool(false), Tick(2)));
        block.step(&io, Tick(2)).unwrap();
        let out = output(&io);
        assert_eq!(out.value, Value::Float(10.0));
        assert_eq!(out.quality, Quality::Good);
    }

    #[test]
    fn builds_from_parameter_map() {
        let instance = ComponentInstance {
            id: ComponentId(8),
            kind: OverrideSelect::KIND.to_string(),
            parameters: Parameters::new(),
            ports: BTreeMap::new(),
        };
        let mut block = OverrideSelect::from_parameters(
            "ovr",
            CONTROL,
            OPERATOR,
            SELECT,
            OUT,
            &instance.parameters,
        )
        .unwrap();
        let io = io(true);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(output(&io).value, Value::Float(20.0));
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "ovr");
        assert_eq!(descriptor.kind, OverrideSelect::KIND);
        assert_eq!(descriptor.label, "ovr");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "control".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                },
                PortDescriptor {
                    name: "operator".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Setpoint),
                },
                PortDescriptor {
                    name: "select".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                },
                PortDescriptor {
                    name: "out".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Output),
                },
            ]
        );
        // Drift guard: `from_parameters` reads no keys.
        assert!(descriptor.parameters.is_empty());
    }
}
