//! Analog output: engineering-unit to raw-range scaling — the inverse of
//! [`AnalogInput`](crate::AnalogInput).

use crate::analog_input::Scaling;
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

/// A value type an [`AnalogOutput`] can write as its raw output: integer
/// converter counts (`i64`) or a directly floating raw value (`f64`).
///
/// The trait is sealed to those two types, mirroring
/// [`RawInput`](crate::RawInput) on the input side; a `Bool` raw output is
/// meaningless for scaling.
pub trait RawOutput: PointType + sealed::Sealed {
    /// The scaled `f64` in the output representation. `i64` rounds to the
    /// nearest count and saturates at the type's range.
    fn from_f64(value: f64) -> Self;
}

impl RawOutput for i64 {
    fn from_f64(value: f64) -> Self {
        value.round() as i64
    }
}

impl RawOutput for f64 {
    fn from_f64(value: f64) -> Self {
        value
    }
}

/// An analog output channel: scales an engineering `In` point onto a raw
/// `Out` point — the inverse of [`AnalogInput`](crate::AnalogInput).
///
/// Each step reads `eng`, clamps it into the configured engineering range,
/// applies the inverse of the [`Scaling`] map, clamps the result into the
/// raw range (absorbing float error at the boundaries), and writes `raw`.
/// The written sample's quality is the input's merged with the scaling's
/// own verdict: an engineering value outside the configured range clamps
/// and marks the output `Uncertain(OutOfRange)`, a `NaN` input marks it
/// `Bad(DeviceFault)`, and any non-`Good` input quality always propagates
/// through.
///
/// Declared I/O: `eng` (`In`, `Float`) and `raw` (`Out`, `R`).
///
/// Parameters: `raw_min`, `raw_max`, `eng_min`, `eng_max` — all required,
/// finite, `Float` or losslessly representable `Int`. Both ranges must
/// have non-zero span: the inverse map divides by the engineering span,
/// and a raw range of one value is not an output channel.
#[derive(Debug)]
pub struct AnalogOutput<R: RawOutput = f64> {
    name: String,
    eng: PointId,
    raw: PointId,
    scaling: Scaling,
    marker: PhantomData<fn() -> R>,
}

impl<R: RawOutput> AnalogOutput<R> {
    /// The component-kind string the model-driven registry maps onto this
    /// type's constructor.
    pub const KIND: &'static str = "analog-output";

    /// Builds the component from explicit points and scaling, or reports
    /// the scaling's inconsistency as a [`ParameterError`].
    pub fn new(
        name: impl Into<String>,
        eng: PointId,
        raw: PointId,
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
        if scaling.eng_min == scaling.eng_max {
            return Err(params::invalid(
                &name,
                "eng_max",
                "engineering range must have non-zero span".to_string(),
            ));
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
            eng,
            raw,
            scaling,
            marker: PhantomData,
        })
    }

    /// Builds the component from a plant-model parameter map, reading the
    /// parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        eng: PointId,
        raw: PointId,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let scaling = Scaling {
            raw_min: params::required_f64(&name, parameters, "raw_min")?,
            raw_max: params::required_f64(&name, parameters, "raw_max")?,
            eng_min: params::required_f64(&name, parameters, "eng_min")?,
            eng_max: params::required_f64(&name, parameters, "eng_max")?,
        };
        Self::new(name, eng, raw, scaling)
    }
}

impl<R: RawOutput> Component for AnalogOutput<R> {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("eng", self.eng),
            IoRequirement::output::<R>("raw", self.raw),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let sample = io.read_typed::<f64>(self.eng)?;
        let eng = sample.value;
        let (lo, hi) = self.scaling.eng_bounds();
        let clamped = eng.clamp(lo, hi);
        let quality = if eng.is_nan() {
            sample
                .quality
                .merge(Quality::Bad(QualityReason::DeviceFault))
        } else if clamped != eng {
            sample
                .quality
                .merge(Quality::Uncertain(QualityReason::OutOfRange))
        } else {
            sample.quality
        };
        let (raw_lo, raw_hi) = self.scaling.raw_bounds();
        let raw = self.scaling.apply_inverse(clamped).clamp(raw_lo, raw_hi);
        io.write_sample(
            self.raw,
            Sample::new(R::from_f64(raw).into_value(), quality, tick),
        )?;
        Ok(())
    }

    /// Describes the channel: `eng` is the demanded engineering value the
    /// block drives toward, `raw` the field value it writes; the four
    /// shared scaling parameters.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[("eng", PortRole::Setpoint), ("raw", PortRole::Output)],
            describe::scaling_parameters(),
        )
    }

    /// Tunes one scaling bound at the scan boundary.
    ///
    /// The executor pre-checks the descriptor — declared name, `Float`
    /// kind, and the `FINITE_F64` bound — so this hook owns the
    /// cross-parameter invariants `eng_min != eng_max` and
    /// `raw_min != raw_max`: a bound that would flatten either range is
    /// [`CommandError::InvalidParameter`] and changes nothing.
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
        if scaling.eng_min == scaling.eng_max || scaling.raw_min == scaling.raw_max {
            return Err(params::invalid_parameter(
                &self.name,
                parameter,
                "engineering and raw ranges must have non-zero span",
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
        if scaling.eng_min == scaling.eng_max {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "eng_max".to_string(),
                value: Value::Float(scaling.eng_max),
            });
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
    use crate::AnalogInput;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, ParameterDescriptor, PortDescriptor, Quality, Value, ValueKind};
    use dcs_model::{ComponentId, ComponentInstance};
    use std::collections::BTreeMap;

    const ENG: PointId = PointId(50);
    const RAW: PointId = PointId(51);

    /// The inverse of the test `AnalogInput`: 0-100 engineering units onto
    /// a 4-20 mA style channel.
    fn component() -> AnalogOutput<f64> {
        AnalogOutput::new(
            "ao",
            ENG,
            RAW,
            Scaling {
                raw_min: 4.0,
                raw_max: 20.0,
                eng_min: 0.0,
                eng_max: 100.0,
            },
        )
        .unwrap()
    }

    fn io(eng_sample: Sample) -> TestIo {
        TestIo::new(&[
            (ENG, Direction::In, eng_sample),
            (
                RAW,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
        ])
    }

    fn output(io: &TestIo) -> Sample {
        io.written(RAW).unwrap()
    }

    #[test]
    fn scales_engineering_range_onto_raw_range() {
        let mut ao = component();
        for (eng, expected) in [(0.0, 4.0), (50.0, 12.0), (100.0, 20.0), (25.0, 8.0)] {
            let io = io(Sample::good(Value::Float(eng), Tick::ZERO));
            ao.step(&io, Tick(1)).unwrap();
            let out = output(&io);
            assert_eq!(out.value, Value::Float(expected), "eng={eng}");
            assert_eq!(out.quality, Quality::Good, "eng={eng}");
            assert_eq!(out.tick, Tick(1));
        }
    }

    #[test]
    fn clamps_engineering_outside_range_and_marks_uncertain() {
        let mut ao = component();
        for (eng, expected) in [
            (-10.0, 4.0),
            (150.0, 20.0),
            (f64::NEG_INFINITY, 4.0),
            (f64::INFINITY, 20.0),
        ] {
            let io = io(Sample::good(Value::Float(eng), Tick::ZERO));
            ao.step(&io, Tick(1)).unwrap();
            let out = output(&io);
            assert_eq!(out.value, Value::Float(expected), "eng={eng}");
            assert_eq!(
                out.quality,
                Quality::Uncertain(QualityReason::OutOfRange),
                "eng={eng}"
            );
        }
        // Exact range ends stay inside and keep Good quality.
        for eng in [0.0, 100.0] {
            let io = io(Sample::good(Value::Float(eng), Tick::ZERO));
            ao.step(&io, Tick(1)).unwrap();
            assert_eq!(output(&io).quality, Quality::Good, "eng={eng}");
        }
    }

    #[test]
    fn bad_input_quality_propagates_to_output() {
        let mut ao = component();
        let io = io(Sample::new(
            Value::Float(50.0),
            Quality::Bad(QualityReason::CommunicationFault),
            Tick::ZERO,
        ));
        ao.step(&io, Tick(1)).unwrap();
        let out = output(&io);
        assert_eq!(out.value, Value::Float(12.0));
        assert_eq!(out.quality, Quality::Bad(QualityReason::CommunicationFault));
    }

    #[test]
    fn nan_engineering_marks_output_bad() {
        let mut ao = component();
        let io = io(Sample::good(Value::Float(f64::NAN), Tick::ZERO));
        ao.step(&io, Tick(1)).unwrap();
        assert_eq!(
            output(&io).quality,
            Quality::Bad(QualityReason::DeviceFault)
        );
    }

    #[test]
    fn integer_raw_output_rounds_to_counts() {
        // 0-100 engineering units onto 0-27648 DAC counts.
        let mut ao = AnalogOutput::<i64>::new(
            "ao",
            ENG,
            RAW,
            Scaling {
                raw_min: 0.0,
                raw_max: 27648.0,
                eng_min: 0.0,
                eng_max: 100.0,
            },
        )
        .unwrap();
        let requirements = ao.io_requirements();
        assert_eq!(requirements[1].kind, dcs_core::ValueKind::Int);

        let io = TestIo::new(&[
            (
                ENG,
                Direction::In,
                Sample::good(Value::Float(25.0), Tick::ZERO),
            ),
            (RAW, Direction::Out, Sample::good(Value::Int(0), Tick::ZERO)),
        ]);
        ao.step(&io, Tick(1)).unwrap();
        assert_eq!(io.written(RAW).unwrap().value, Value::Int(6912));
    }

    #[test]
    fn inverts_analog_input_scaling_within_tolerance() {
        let scaling = Scaling {
            raw_min: 4.0,
            raw_max: 20.0,
            eng_min: 0.0,
            eng_max: 100.0,
        };
        let mut ai = AnalogInput::<f64>::new("ai", RAW, ENG, scaling).unwrap();
        let mut ao = component();
        // Round-trip: raw -> eng through `AnalogInput`, eng -> raw through
        // `AnalogOutput`. Stated tolerance: 1e-9 raw units over the 16-unit
        // span — float error in the two linear maps is far below it.
        for raw in [4.0, 7.3, 12.0, 19.99, 20.0] {
            let ai_io = TestIo::new(&[
                (
                    RAW,
                    Direction::In,
                    Sample::good(Value::Float(raw), Tick::ZERO),
                ),
                (
                    ENG,
                    Direction::Out,
                    Sample::good(Value::Float(0.0), Tick::ZERO),
                ),
            ]);
            ai.step(&ai_io, Tick(1)).unwrap();
            let eng = ai_io.written(ENG).unwrap();

            let ao_io = io(eng);
            ao.step(&ao_io, Tick(1)).unwrap();
            let out = output(&ao_io);
            let Value::Float(roundtripped) = out.value else {
                panic!("raw must be Float")
            };
            assert!(
                (roundtripped - raw).abs() < 1e-9,
                "raw={raw} roundtripped={roundtripped}"
            );
        }
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
            id: ComponentId(5),
            kind: AnalogOutput::<f64>::KIND.to_string(),
            parameters,
            ports: BTreeMap::new(),
        };
        let mut ao =
            AnalogOutput::<f64>::from_parameters("ao", ENG, RAW, &instance.parameters).unwrap();
        let io = io(Sample::good(Value::Float(50.0), Tick::ZERO));
        ao.step(&io, Tick(1)).unwrap();
        assert_eq!(output(&io).value, Value::Float(12.0));

        assert_eq!(
            AnalogOutput::<f64>::from_parameters("ao", ENG, RAW, &Parameters::new()).unwrap_err(),
            ParameterError::Missing {
                component: "ao".to_string(),
                parameter: "raw_min".to_string(),
            }
        );
    }

    #[test]
    fn degenerate_or_non_finite_scaling_is_rejected() {
        let flat_eng = Scaling {
            eng_min: 50.0,
            eng_max: 50.0,
            ..component().scaling
        };
        assert!(matches!(
            AnalogOutput::<f64>::new("ao", ENG, RAW, flat_eng),
            Err(ParameterError::Invalid { ref parameter, .. }) if parameter == "eng_max"
        ));

        let flat_raw = Scaling {
            raw_min: 4.0,
            raw_max: 4.0,
            ..component().scaling
        };
        assert!(matches!(
            AnalogOutput::<f64>::new("ao", ENG, RAW, flat_raw),
            Err(ParameterError::Invalid { ref parameter, .. }) if parameter == "raw_max"
        ));

        let non_finite = Scaling {
            eng_max: f64::INFINITY,
            ..component().scaling
        };
        assert!(matches!(
            AnalogOutput::<f64>::new("ao", ENG, RAW, non_finite),
            Err(ParameterError::Invalid { .. })
        ));
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "ao");
        assert_eq!(descriptor.kind, AnalogOutput::<f64>::KIND);
        assert_eq!(descriptor.label, "ao");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "eng".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Setpoint),
                    point: None,
                },
                PortDescriptor {
                    name: "raw".to_string(),
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
