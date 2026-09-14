//! Alarm monitor: high/low limit checking with hysteresis on an analog
//! signal.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, PointId, PortRole, Quality, QualityReason, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// Limit and deadband parameters for [`AlarmMonitor`].
///
/// `low < high` must hold so the two alarm regions cannot overlap, and
/// `hysteresis` must be non-negative. All fields must be finite.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct AlarmLimits {
    /// Trip threshold for the low alarm.
    pub low: f64,
    /// Trip threshold for the high alarm.
    pub high: f64,
    /// The deadband keeping a tripped alarm active while the input recedes
    /// toward its limit. `0.0` makes trip and clear thresholds coincide.
    pub hysteresis: f64,
}

/// Which limit, if any, currently holds the alarm.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Alarm {
    Clear,
    High,
    Low,
}

/// An alarm monitor: reads an analog `In` point and drives a boolean
/// `alarm` output against configurable high and low limits.
///
/// The high alarm trips when the input reaches `high` and clears once it
/// falls strictly below `high - hysteresis`; the low alarm trips when the
/// input reaches `low` and clears once it rises strictly above
/// `low + hysteresis`. Inside a deadband the alarm holds its current
/// state. An input that plunges past the opposite limit transitions the
/// alarm directly, e.g. high to low.
///
/// The alarm is evaluated every scan regardless of input quality; the
/// output sample carries the input's quality, so a degraded reading
/// produces a degraded alarm indication rather than a hidden one. A `NaN`
/// input fails every comparison, holding the current alarm state, and
/// additionally marks the output `Bad(DeviceFault)`.
///
/// Declared I/O: `in` (`In`, `Float`) and `alarm` (`Out`, `Bool`).
///
/// Parameters: `high_limit` and `low_limit` — required finite `Float` or
/// losslessly representable `Int`, `low_limit < high_limit`;
/// `hysteresis` — optional, finite and non-negative, default `0.0`.
#[derive(Debug)]
pub struct AlarmMonitor {
    name: String,
    input: PointId,
    alarm: PointId,
    limits: AlarmLimits,
    state: Alarm,
}

impl AlarmMonitor {
    /// The component-kind string the model-driven registry maps onto this
    /// type's constructor.
    pub const KIND: &'static str = "alarm-monitor";

    /// Builds the component from explicit points and limits, or reports
    /// the limits' inconsistency as a [`ParameterError`].
    pub fn new(
        name: impl Into<String>,
        input: PointId,
        alarm: PointId,
        limits: AlarmLimits,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        for (parameter, value) in [
            ("low_limit", limits.low),
            ("high_limit", limits.high),
            ("hysteresis", limits.hysteresis),
        ] {
            if !value.is_finite() {
                return Err(params::invalid(
                    &name,
                    parameter,
                    "must be finite".to_string(),
                ));
            }
        }
        if limits.low >= limits.high {
            return Err(params::invalid(
                &name,
                "high_limit",
                "limits require low_limit < high_limit".to_string(),
            ));
        }
        if limits.hysteresis < 0.0 {
            return Err(params::invalid(
                &name,
                "hysteresis",
                "must be non-negative".to_string(),
            ));
        }
        Ok(Self {
            name,
            input,
            alarm,
            limits,
            state: Alarm::Clear,
        })
    }

    /// Builds the component from a plant-model parameter map, reading the
    /// parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        input: PointId,
        alarm: PointId,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let limits = AlarmLimits {
            low: params::required_f64(&name, parameters, "low_limit")?,
            high: params::required_f64(&name, parameters, "high_limit")?,
            hysteresis: params::optional_f64(&name, parameters, "hysteresis")?.unwrap_or(0.0),
        };
        Self::new(name, input, alarm, limits)
    }
}

impl Component for AlarmMonitor {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("in", self.input),
            IoRequirement::output::<bool>("alarm", self.alarm),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let sample = io.read_typed::<f64>(self.input)?;
        let pv = sample.value;
        let limits = self.limits;
        self.state = if pv.is_nan() {
            // A NaN input satisfies no comparison; hold the current state.
            self.state
        } else {
            match self.state {
                // A tripped alarm holds until the input has receded past
                // its limit by the hysteresis.
                Alarm::High if pv >= limits.high - limits.hysteresis => Alarm::High,
                Alarm::Low if pv <= limits.low + limits.hysteresis => Alarm::Low,
                _ if pv >= limits.high => Alarm::High,
                _ if pv <= limits.low => Alarm::Low,
                _ => Alarm::Clear,
            }
        };
        let quality = if pv.is_nan() {
            sample
                .quality
                .merge(Quality::Bad(QualityReason::DeviceFault))
        } else {
            sample.quality
        };
        io.write_sample(
            self.alarm,
            Sample::new(Value::Bool(self.state != Alarm::Clear), quality, tick),
        )?;
        Ok(())
    }

    /// Describes the monitor: `in` is the measured process value checked
    /// against the limits, `alarm` the reported trip state; the limit and
    /// hysteresis parameters `from_parameters` reads.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[("in", PortRole::ProcessValue), ("alarm", PortRole::Status)],
            vec![
                describe::parameter("low_limit", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("high_limit", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter(
                    "hysteresis",
                    ValueKind::Float,
                    Some(describe::NONNEGATIVE_F64),
                ),
            ],
        )
    }

    /// Tunes a declared parameter at the scan boundary.
    ///
    /// The executor pre-checks the descriptor — declared name, `Float`
    /// kind, and the declared range — so this hook owns the
    /// cross-parameter invariant `low_limit < high_limit`. A value
    /// breaking it is [`CommandError::InvalidParameter`] and changes
    /// nothing.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        let tuned = params::tune_f64(&self.name, parameter, value)?;
        let mut limits = self.limits;
        match parameter {
            "low_limit" => limits.low = tuned,
            "high_limit" => limits.high = tuned,
            "hysteresis" => limits.hysteresis = tuned,
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        if !tuned.is_finite() {
            return Err(params::invalid_parameter(
                &self.name,
                parameter,
                "must be finite",
            ));
        }
        if limits.low >= limits.high {
            return Err(params::invalid_parameter(
                &self.name,
                parameter,
                "limits require low_limit < high_limit",
            ));
        }
        if limits.hysteresis < 0.0 {
            return Err(params::invalid_parameter(
                &self.name,
                "hysteresis",
                "must be non-negative",
            ));
        }
        self.limits = limits;
        Ok(())
    }

    /// Captures which limit, if any, holds the alarm — the state the
    /// hysteresis bands act on — plus the tuned limits, so a tracking
    /// standby inherits runtime tuning.
    fn capture_state(&self) -> StateMap {
        let mut state = StateMap::new();
        state.insert(
            "state",
            Value::Int(match self.state {
                Alarm::Clear => 0,
                Alarm::High => 1,
                Alarm::Low => 2,
            }),
        );
        state.insert("low_limit", Value::Float(self.limits.low));
        state.insert("high_limit", Value::Float(self.limits.high));
        state.insert("hysteresis", Value::Float(self.limits.hysteresis));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(
            &self.name,
            &["state", "low_limit", "high_limit", "hysteresis"],
        )?;
        let code = state.require_i64(&self.name, "state")?;
        let restored = match code {
            0 => Alarm::Clear,
            1 => Alarm::High,
            2 => Alarm::Low,
            _ => {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: "state".to_string(),
                    value: Value::Int(code),
                });
            }
        };
        let limits = AlarmLimits {
            low: state.require_f64(&self.name, "low_limit")?,
            high: state.require_f64(&self.name, "high_limit")?,
            hysteresis: state.require_f64(&self.name, "hysteresis")?,
        };
        // The same invariants `new` and `apply_parameter` enforce.
        for (field, value) in [
            ("low_limit", limits.low),
            ("high_limit", limits.high),
            ("hysteresis", limits.hysteresis),
        ] {
            if !value.is_finite() {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: field.to_string(),
                    value: Value::Float(value),
                });
            }
        }
        if limits.low >= limits.high || limits.hysteresis < 0.0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "high_limit".to_string(),
                value: Value::Float(limits.high),
            });
        }
        self.limits = limits;
        self.state = restored;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, ParameterDescriptor, PortDescriptor, Quality};
    use dcs_model::{ComponentId, ComponentInstance};
    use std::collections::BTreeMap;

    const IN: PointId = PointId(60);
    const ALARM: PointId = PointId(61);

    /// Limits 10 <= low, 90 <= high with a 5-unit hysteresis.
    fn component() -> AlarmMonitor {
        AlarmMonitor::new(
            "alm",
            IN,
            ALARM,
            AlarmLimits {
                low: 10.0,
                high: 90.0,
                hysteresis: 5.0,
            },
        )
        .unwrap()
    }

    fn io() -> TestIo {
        TestIo::new(&[
            (
                IN,
                Direction::In,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                ALARM,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    fn feed(io: &TestIo, pv: f64, tick: u64) {
        io.feed(IN, Sample::good(Value::Float(pv), Tick(tick)));
    }

    fn alarmed(io: &TestIo) -> bool {
        io.written(ALARM).unwrap().value == Value::Bool(true)
    }

    #[test]
    fn trips_on_high_and_low_limits() {
        let mut block = component();
        let io = io();

        // Inside the limits: clear.
        feed(&io, 50.0, 1);
        block.step(&io, Tick(1)).unwrap();
        assert!(!alarmed(&io));

        // Reaching the high limit trips.
        feed(&io, 90.0, 2);
        block.step(&io, Tick(2)).unwrap();
        assert!(alarmed(&io));

        // Returning inside clears.
        feed(&io, 50.0, 3);
        block.step(&io, Tick(3)).unwrap();
        assert!(!alarmed(&io));

        // Reaching the low limit trips.
        feed(&io, 10.0, 4);
        block.step(&io, Tick(4)).unwrap();
        assert!(alarmed(&io));
    }

    #[test]
    fn hysteresis_holds_then_clears_tripped_alarms() {
        let mut block = component();
        let io = io();

        // Trip high at 90; the deadband holds the alarm down to 85.
        feed(&io, 95.0, 1);
        block.step(&io, Tick(1)).unwrap();
        assert!(alarmed(&io));
        for (tick, pv) in [(2, 87.0), (3, 85.0)] {
            feed(&io, pv, tick);
            block.step(&io, Tick(tick)).unwrap();
            assert!(alarmed(&io), "pv={pv} inside the high deadband");
        }
        // Strictly below 85 clears.
        feed(&io, 84.9, 4);
        block.step(&io, Tick(4)).unwrap();
        assert!(!alarmed(&io));

        // Trip low at 10; the deadband holds the alarm up to 15.
        feed(&io, 5.0, 5);
        block.step(&io, Tick(5)).unwrap();
        assert!(alarmed(&io));
        for (tick, pv) in [(6, 13.0), (7, 15.0)] {
            feed(&io, pv, tick);
            block.step(&io, Tick(tick)).unwrap();
            assert!(alarmed(&io), "pv={pv} inside the low deadband");
        }
        // Strictly above 15 clears.
        feed(&io, 15.1, 8);
        block.step(&io, Tick(8)).unwrap();
        assert!(!alarmed(&io));
    }

    #[test]
    fn zero_hysteresis_trips_and_clears_at_the_limit() {
        let mut block = AlarmMonitor::new(
            "alm",
            IN,
            ALARM,
            AlarmLimits {
                low: 10.0,
                high: 90.0,
                hysteresis: 0.0,
            },
        )
        .unwrap();
        let io = io();
        feed(&io, 90.0, 1);
        block.step(&io, Tick(1)).unwrap();
        assert!(alarmed(&io));
        feed(&io, 89.9, 2);
        block.step(&io, Tick(2)).unwrap();
        assert!(!alarmed(&io));
    }

    #[test]
    fn bad_quality_input_still_evaluates_and_propagates_quality() {
        let mut block = component();
        let io = io();
        io.feed(
            IN,
            Sample::new(
                Value::Float(95.0),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(1),
            ),
        );
        block.step(&io, Tick(1)).unwrap();
        let out = io.written(ALARM).unwrap();
        assert_eq!(out.value, Value::Bool(true));
        assert_eq!(out.quality, Quality::Bad(QualityReason::CommunicationFault));
    }

    #[test]
    fn nan_input_holds_state_and_marks_bad() {
        let mut block = component();
        let io = io();

        feed(&io, 95.0, 1);
        block.step(&io, Tick(1)).unwrap();
        assert!(alarmed(&io));

        feed(&io, f64::NAN, 2);
        block.step(&io, Tick(2)).unwrap();
        let out = io.written(ALARM).unwrap();
        assert_eq!(out.value, Value::Bool(true));
        assert_eq!(out.quality, Quality::Bad(QualityReason::DeviceFault));
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [
            ("low_limit".to_string(), Value::Int(10)),
            ("high_limit".to_string(), Value::Int(90)),
            ("hysteresis".to_string(), Value::Float(5.0)),
        ]
        .into_iter()
        .collect();
        let instance = ComponentInstance {
            id: ComponentId(6),
            kind: AlarmMonitor::KIND.to_string(),
            parameters,
            ports: BTreeMap::new(),
        };
        let mut block =
            AlarmMonitor::from_parameters("alm", IN, ALARM, &instance.parameters).unwrap();
        assert_eq!(
            block.limits,
            AlarmLimits {
                low: 10.0,
                high: 90.0,
                hysteresis: 5.0,
            }
        );

        let io = io();
        feed(&io, 95.0, 1);
        block.step(&io, Tick(1)).unwrap();
        assert!(alarmed(&io));

        // hysteresis is optional; the limits are not.
        assert!(matches!(
            AlarmMonitor::from_parameters("alm", IN, ALARM, &Parameters::new()).unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "low_limit"
        ));
    }

    #[test]
    fn invalid_limits_are_rejected() {
        let reversed = AlarmLimits {
            low: 90.0,
            high: 10.0,
            hysteresis: 5.0,
        };
        assert!(matches!(
            AlarmMonitor::new("alm", IN, ALARM, reversed),
            Err(ParameterError::Invalid { ref parameter, .. }) if parameter == "high_limit"
        ));

        let negative_hysteresis = AlarmLimits {
            hysteresis: -1.0,
            ..component().limits
        };
        assert!(matches!(
            AlarmMonitor::new("alm", IN, ALARM, negative_hysteresis),
            Err(ParameterError::Invalid { ref parameter, .. }) if parameter == "hysteresis"
        ));

        let non_finite = AlarmLimits {
            high: f64::INFINITY,
            ..component().limits
        };
        assert!(matches!(
            AlarmMonitor::new("alm", IN, ALARM, non_finite),
            Err(ParameterError::Invalid { .. })
        ));
    }

    #[test]
    fn apply_parameter_tunes_limits_and_hysteresis() {
        let mut block = component();
        block
            .apply_parameter("high_limit", Value::Float(80.0))
            .unwrap();
        block
            .apply_parameter("hysteresis", Value::Float(2.0))
            .unwrap();
        assert_eq!(block.limits.high, 80.0);
        assert_eq!(block.limits.hysteresis, 2.0);

        // The tuned limits take effect on the next step.
        let io = io();
        feed(&io, 85.0, 1);
        block.step(&io, Tick(1)).unwrap();
        assert!(alarmed(&io));

        // An undeclared name is a named rejection naming the component.
        assert_eq!(
            block
                .apply_parameter("debounce", Value::Float(1.0))
                .unwrap_err(),
            CommandError::UnknownParameter {
                component: "alm".to_string(),
                parameter: "debounce".to_string(),
            }
        );

        // Crossing low over high refuses and changes nothing — the
        // cross-parameter invariant no declared range can express.
        assert!(matches!(
            block
                .apply_parameter("low_limit", Value::Float(95.0))
                .unwrap_err(),
            CommandError::InvalidParameter {
                ref component,
                ref parameter,
                ..
            } if component == "alm" && parameter == "low_limit"
        ));
        assert_eq!(block.limits.low, 10.0);
    }

    #[test]
    fn tuned_limits_ride_the_checkpoint() {
        let mut block = component();
        let active_io = io();
        feed(&active_io, 95.0, 1);
        block.step(&active_io, Tick(1)).unwrap();
        block
            .apply_parameter("high_limit", Value::Float(100.0))
            .unwrap();

        let state = block.capture_state();
        assert_eq!(state.get("high_limit"), Some(Value::Float(100.0)));
        // The banked alarm state rides along with the tuning.
        assert_eq!(state.get("state"), Some(Value::Int(1)));

        let mut standby = component();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.limits.high, 100.0);
        // Restore mid-trip: 95 is inside the new limit, but the captured
        // high-alarm state survives into the next step's hysteresis.
        let standby_io = io();
        feed(&standby_io, 99.0, 2);
        standby.step(&standby_io, Tick(2)).unwrap();
        assert!(alarmed(&standby_io));
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "alm");
        assert_eq!(descriptor.kind, AlarmMonitor::KIND);
        assert_eq!(descriptor.label, "alm");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "in".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "alarm".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
            ]
        );
        // Drift guard: the descriptor's parameter names are exactly the
        // keys `from_parameters` reads.
        assert_eq!(
            descriptor.parameters,
            [
                ParameterDescriptor {
                    name: "low_limit".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "high_limit".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "hysteresis".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::NONNEGATIVE_F64),
                },
            ]
        );
    }
}
