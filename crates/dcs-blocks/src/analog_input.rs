//! Analog input: linear raw-range to engineering-unit scaling.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, PointId, PointType, PortRole, Quality, QualityReason,
    Sample, StateError, StateMap, Tick, Value,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};
use std::marker::PhantomData;

mod sealed {
    pub trait Sealed {}
    impl Sealed for i64 {}
    impl Sealed for f64 {}
}

/// A value type an [`AnalogInput`] can read as its raw input: integer
/// converter counts (`i64`) or an already-floating raw value (`f64`).
///
/// The trait is sealed to those two types; a `Bool` raw input is
/// meaningless for scaling, and `PointType`'s own seal keeps the logical
/// I/O type set closed.
pub trait RawInput: PointType + sealed::Sealed {
    /// The value as `f64` for the scaling computation.
    fn as_f64(self) -> f64;
}

impl RawInput for i64 {
    fn as_f64(self) -> f64 {
        self as f64
    }
}

impl RawInput for f64 {
    fn as_f64(self) -> f64 {
        self
    }
}

/// A linear map from a raw range onto an engineering range.
///
/// `eng = eng_min + (raw - raw_min) · (eng_max - eng_min) / (raw_max -
/// raw_min)`. Either range may be inverted (`raw_min > raw_max` or
/// `eng_min > eng_max`); `raw_min` must differ from `raw_max` and all
/// bounds must be finite.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Scaling {
    /// The raw value producing `eng_min`.
    pub raw_min: f64,
    /// The raw value producing `eng_max`.
    pub raw_max: f64,
    /// The engineering value at `raw_min`.
    pub eng_min: f64,
    /// The engineering value at `raw_max`.
    pub eng_max: f64,
}

impl Scaling {
    /// The raw-range bounds in ascending order.
    pub(crate) fn raw_bounds(&self) -> (f64, f64) {
        (
            self.raw_min.min(self.raw_max),
            self.raw_min.max(self.raw_max),
        )
    }

    /// The engineering-range bounds in ascending order.
    pub(crate) fn eng_bounds(&self) -> (f64, f64) {
        (
            self.eng_min.min(self.eng_max),
            self.eng_min.max(self.eng_max),
        )
    }

    /// Applies the map to a raw value already inside the range.
    fn apply(&self, raw: f64) -> f64 {
        self.eng_min
            + (raw - self.raw_min) * (self.eng_max - self.eng_min) / (self.raw_max - self.raw_min)
    }

    /// Applies the inverse map to an engineering value inside the range:
    /// the raw value `apply` would map back onto `eng`.
    pub(crate) fn apply_inverse(&self, eng: f64) -> f64 {
        self.raw_min
            + (eng - self.eng_min) * (self.raw_max - self.raw_min) / (self.eng_max - self.eng_min)
    }
}

/// An analog input channel: scales a raw `In` point onto an engineering
/// `Out` point.
///
/// Each step reads `raw`, clamps it into the configured raw range, scales
/// the result linearly onto the engineering range, and writes `out`. The
/// output's quality is the input's merged with the scaling's own verdict:
/// a raw value outside the configured range clamps and marks the output
/// `Uncertain(OutOfRange)`, a `NaN` raw value marks it `Bad(DeviceFault)`,
/// and any non-`Good` input quality always propagates through.
///
/// Declared I/O: `raw` (`In`, `R`) and `out` (`Out`, `Float`).
///
/// Parameters: `raw_min`, `raw_max`, `eng_min`, `eng_max` — all required,
/// finite, `Float` or losslessly representable `Int`.
#[derive(Debug)]
pub struct AnalogInput<R: RawInput = f64> {
    name: String,
    raw: PointId,
    out: PointId,
    scaling: Scaling,
    marker: PhantomData<fn() -> R>,
}

impl<R: RawInput> AnalogInput<R> {
    /// The component-kind string the model-driven registry maps onto this
    /// type's constructor.
    pub const KIND: &'static str = "analog-input";

    /// Builds the component from explicit points and scaling, or reports
    /// the scaling's inconsistency as a [`ParameterError`].
    pub fn new(
        name: impl Into<String>,
        raw: PointId,
        out: PointId,
        scaling: Scaling,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        for (parameter, value) in [
            ("raw_min", scaling.raw_min),
            ("raw_max", scaling.raw_max),
            ("eng_min", scaling.eng_min),
            ("eng_max", scaling.eng_max),
        ] {
            if !value.is_finite() {
                return Err(params::invalid(
                    &name,
                    parameter,
                    "must be finite".to_string(),
                ));
            }
        }
        if scaling.raw_min == scaling.raw_max {
            return Err(params::invalid(
                &name,
                "raw_max",
                "raw range must have non-zero span".to_string(),
            ));
        }
        Ok(Self {
            name,
            raw,
            out,
            scaling,
            marker: PhantomData,
        })
    }

    /// Builds the component from a plant-model parameter map, reading the
    /// parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        raw: PointId,
        out: PointId,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let scaling = Scaling {
            raw_min: params::required_f64(&name, parameters, "raw_min")?,
            raw_max: params::required_f64(&name, parameters, "raw_max")?,
            eng_min: params::required_f64(&name, parameters, "eng_min")?,
            eng_max: params::required_f64(&name, parameters, "eng_max")?,
        };
        Self::new(name, raw, out, scaling)
    }
}

impl<R: RawInput> Component for AnalogInput<R> {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<R>("raw", self.raw),
            IoRequirement::output::<f64>("out", self.out),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let sample = io.read_typed::<R>(self.raw)?;
        let raw = sample.value.as_f64();
        let (lo, hi) = self.scaling.raw_bounds();
        let clamped = raw.clamp(lo, hi);
        let quality = if raw.is_nan() {
            sample
                .quality
                .merge(Quality::Bad(QualityReason::DeviceFault))
        } else if clamped != raw {
            sample
                .quality
                .merge(Quality::Uncertain(QualityReason::OutOfRange))
        } else {
            sample.quality
        };
        io.write_sample(
            self.out,
            Sample::new(Value::Float(self.scaling.apply(clamped)), quality, tick),
        )?;
        Ok(())
    }

    /// Describes the channel: `raw` is the measured process value the
    /// block scales, `out` the engineering value it publishes; the four
    /// shared scaling parameters.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[("raw", PortRole::ProcessValue), ("out", PortRole::Output)],
            describe::scaling_parameters(),
        )
    }

    /// Tunes one scaling bound at the scan boundary.
    ///
    /// The executor pre-checks the descriptor — declared name, `Float`
    /// kind, and the `FINITE_F64` bound — so this hook owns the
    /// cross-parameter invariant `raw_min != raw_max`: a bound that
    /// would flatten the raw range is [`CommandError::InvalidParameter`]
    /// and changes nothing.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        let tuned = params::tune_f64(&self.name, parameter, value)?;
        let mut scaling = self.scaling;
        match parameter {
            "raw_min" => scaling.raw_min = tuned,
            "raw_max" => scaling.raw_max = tuned,
            "eng_min" => scaling.eng_min = tuned,
            "eng_max" => scaling.eng_max = tuned,
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        if !tuned.is_finite() {
            return Err(params::invalid_parameter(
                &self.name,
                parameter,
                "must be finite",
            ));
        }
        if scaling.raw_min == scaling.raw_max {
            return Err(params::invalid_parameter(
                &self.name,
                parameter,
                "raw range must have non-zero span",
            ));
        }
        self.scaling = scaling;
        Ok(())
    }

    /// Captures the tuned scaling bounds — runtime tuning is run state
    /// a tracking standby must inherit.
    fn capture_state(&self) -> StateMap {
        let mut state = StateMap::new();
        state.insert("raw_min", Value::Float(self.scaling.raw_min));
        state.insert("raw_max", Value::Float(self.scaling.raw_max));
        state.insert("eng_min", Value::Float(self.scaling.eng_min));
        state.insert("eng_max", Value::Float(self.scaling.eng_max));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(&self.name, &["raw_min", "raw_max", "eng_min", "eng_max"])?;
        let scaling = Scaling {
            raw_min: state.require_f64(&self.name, "raw_min")?,
            raw_max: state.require_f64(&self.name, "raw_max")?,
            eng_min: state.require_f64(&self.name, "eng_min")?,
            eng_max: state.require_f64(&self.name, "eng_max")?,
        };
        for (field, value) in [
            ("raw_min", scaling.raw_min),
            ("raw_max", scaling.raw_max),
            ("eng_min", scaling.eng_min),
            ("eng_max", scaling.eng_max),
        ] {
            if !value.is_finite() {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: field.to_string(),
                    value: Value::Float(value),
                });
            }
        }
        if scaling.raw_min == scaling.raw_max {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "raw_max".to_string(),
                value: Value::Float(scaling.raw_max),
            });
        }
        self.scaling = scaling;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, ParameterDescriptor, PortDescriptor, Quality, ValueKind};
    use dcs_model::{ComponentId, ComponentInstance};
    use std::collections::BTreeMap;

    const RAW: PointId = PointId(1);
    const OUT: PointId = PointId(2);

    /// A 4-20 mA style channel scaled onto 0-100 engineering units.
    fn component() -> AnalogInput<f64> {
        AnalogInput::new(
            "ai",
            RAW,
            OUT,
            Scaling {
                raw_min: 4.0,
                raw_max: 20.0,
                eng_min: 0.0,
                eng_max: 100.0,
            },
        )
        .unwrap()
    }

    fn io(raw_sample: Sample) -> TestIo {
        TestIo::new(&[
            (RAW, Direction::In, raw_sample),
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
    fn scales_raw_range_onto_engineering_range() {
        let mut ai = component();
        for (raw, expected) in [(4.0, 0.0), (12.0, 50.0), (20.0, 100.0), (8.0, 25.0)] {
            let io = io(Sample::good(Value::Float(raw), Tick::ZERO));
            ai.step(&io, Tick(1)).unwrap();
            let out = output(&io);
            assert_eq!(out.value, Value::Float(expected), "raw={raw}");
            assert_eq!(out.quality, Quality::Good, "raw={raw}");
            assert_eq!(out.tick, Tick(1));
        }
    }

    #[test]
    fn clamps_raw_outside_range_and_marks_uncertain() {
        let mut ai = component();
        for (raw, expected) in [(0.0, 0.0), (25.0, 100.0), (f64::INFINITY, 100.0)] {
            let io = io(Sample::good(Value::Float(raw), Tick::ZERO));
            ai.step(&io, Tick(1)).unwrap();
            let out = output(&io);
            assert_eq!(out.value, Value::Float(expected), "raw={raw}");
            assert_eq!(
                out.quality,
                Quality::Uncertain(QualityReason::OutOfRange),
                "raw={raw}"
            );
        }
        // Exact range ends stay inside and keep Good quality.
        for raw in [4.0, 20.0] {
            let io = io(Sample::good(Value::Float(raw), Tick::ZERO));
            ai.step(&io, Tick(1)).unwrap();
            assert_eq!(output(&io).quality, Quality::Good, "raw={raw}");
        }
    }

    #[test]
    fn bad_input_quality_propagates_to_output() {
        let mut ai = component();
        let io = io(Sample::new(
            Value::Float(12.0),
            Quality::Bad(QualityReason::DeviceFault),
            Tick::ZERO,
        ));
        ai.step(&io, Tick(1)).unwrap();
        let out = output(&io);
        assert_eq!(out.value, Value::Float(50.0));
        assert_eq!(out.quality, Quality::Bad(QualityReason::DeviceFault));
    }

    #[test]
    fn nan_raw_marks_output_bad() {
        let mut ai = component();
        let io = io(Sample::good(Value::Float(f64::NAN), Tick::ZERO));
        ai.step(&io, Tick(1)).unwrap();
        let out = output(&io);
        assert_eq!(out.quality, Quality::Bad(QualityReason::DeviceFault));
    }

    #[test]
    fn integer_raw_input_scales() {
        // 0-27648 ADC counts onto 0-100 engineering units.
        let mut ai = AnalogInput::<i64>::new(
            "ai",
            RAW,
            OUT,
            Scaling {
                raw_min: 0.0,
                raw_max: 27648.0,
                eng_min: 0.0,
                eng_max: 100.0,
            },
        )
        .unwrap();
        let requirements = ai.io_requirements();
        assert_eq!(requirements[0].kind, dcs_core::ValueKind::Int);

        let io = TestIo::new(&[
            (
                RAW,
                Direction::In,
                Sample::good(Value::Int(6912), Tick::ZERO),
            ),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
        ]);
        ai.step(&io, Tick(1)).unwrap();
        assert_eq!(output(&io).value, Value::Float(25.0));
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [
            ("raw_min".to_string(), Value::Int(4)),
            ("raw_max".to_string(), Value::Int(20)),
            ("eng_min".to_string(), Value::Float(0.0)),
            ("eng_max".to_string(), Value::Float(100.0)),
        ]
        .into_iter()
        .collect();
        let instance = ComponentInstance {
            id: ComponentId(1),
            kind: "analog-input".to_string(),
            parameters,
            ports: BTreeMap::new(),
        };
        let mut ai =
            AnalogInput::<f64>::from_parameters("ai", RAW, OUT, &instance.parameters).unwrap();
        let io = io(Sample::good(Value::Float(12.0), Tick::ZERO));
        ai.step(&io, Tick(1)).unwrap();
        assert_eq!(output(&io).value, Value::Float(50.0));
    }

    #[test]
    fn parameter_map_errors_name_component_and_parameter() {
        let missing = Parameters::new();
        assert_eq!(
            AnalogInput::<f64>::from_parameters("ai", RAW, OUT, &missing).unwrap_err(),
            ParameterError::Missing {
                component: "ai".to_string(),
                parameter: "raw_min".to_string(),
            }
        );

        let wrong_type: Parameters = [("raw_min".to_string(), Value::Bool(true))]
            .into_iter()
            .collect();
        assert!(matches!(
            AnalogInput::<f64>::from_parameters("ai", RAW, OUT, &wrong_type).unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "raw_min"
        ));
    }

    #[test]
    fn degenerate_or_non_finite_scaling_is_rejected() {
        let flat = Scaling {
            raw_min: 1.0,
            raw_max: 1.0,
            eng_min: 0.0,
            eng_max: 100.0,
        };
        assert!(matches!(
            AnalogInput::<f64>::new("ai", RAW, OUT, flat),
            Err(ParameterError::Invalid { .. })
        ));

        let non_finite = Scaling {
            raw_min: f64::NAN,
            ..component().scaling
        };
        assert!(matches!(
            AnalogInput::<f64>::new("ai", RAW, OUT, non_finite),
            Err(ParameterError::Invalid { .. })
        ));
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "ai");
        assert_eq!(descriptor.kind, AnalogInput::<f64>::KIND);
        assert_eq!(descriptor.label, "ai");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "raw".to_string(),
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
            ]
        );
        // Drift guard: the descriptor's parameter names are exactly the
        // keys `from_parameters` reads.
        assert_eq!(
            descriptor.parameters,
            [
                ParameterDescriptor {
                    name: "raw_min".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "raw_max".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "eng_min".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "eng_max".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
            ]
        );
    }
}
