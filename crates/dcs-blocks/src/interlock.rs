//! Interlock: analog pass-through gated by trip inputs and a permissive,
//! with a configured safe value and a tripped flag.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, PointId, PortRole, Quality, QualityReason, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// An interlock: passes the analog `in` through to `out` while the
/// configured conditions prove pass-through is safe, and drives a
/// configured safe value otherwise.
///
/// Pass-through holds while **all** of these conditions are true:
///
/// - `permissive` reads `true` with [`Quality::Good`] — the enabling
///   condition must be asserted *and* trustworthy;
/// - `in` carries [`Quality::Good`] — an interlock never passes a value
///   it cannot trust downstream;
/// - every `trip_N` input reads `false` with [`Quality::Good`].
///
/// Any other state drives `safe_value` on `out` and raises the `tripped`
/// flag output. The rule is fail-safe throughout: an input whose quality
/// is not `Good` cannot vouch for pass-through whatever its value reads,
/// so a faulted or stale trip input demands the safe state just as an
/// active one does, and an unasserted or untrusted permissive does not
/// permit. A `NaN` on `in` merges `Bad(DeviceFault)` into its quality —
/// matching the crate's non-finite-input convention — and so always
/// trips.
///
/// **Reset rule:** the interlock auto-resets. There is no latch and no
/// reset input; `tripped` is recomputed from the current inputs each
/// scan, so `out` returns to pass-through on the first scan where every
/// condition above holds again. A latching variant would need a dedicated
/// reset port; it is deliberately left out so trip state stays a pure
/// function of the inputs.
///
/// `out` always carries [`Quality::Good`]: while passing it is the
/// (necessarily `Good`) input quality, and while tripped it is the
/// configured safe value — a known constant. The `tripped` flag, also
/// `Good`, reports which state produced the output.
///
/// Declared I/O: `in` (`In`, `Float`), `permissive` (`In`, `Bool`),
/// `trip_1` … `trip_N` (`In`, `Bool`) for each point handed to the
/// constructor, `out` (`Out`, `Float`), and `tripped` (`Out`, `Bool`). An
/// empty trip set is allowed: the permissive and input-quality conditions
/// stay in force.
///
/// Parameters: `safe_value` — required finite `Float` or losslessly
/// representable `Int`, the value driven while tripped.
#[derive(Debug)]
pub struct Interlock {
    name: String,
    input: PointId,
    permissive: PointId,
    trips: Vec<PointId>,
    output: PointId,
    tripped: PointId,
    safe_value: f64,
}

impl Interlock {
    /// The component-kind string the model-driven registry maps onto this
    /// type's constructor.
    pub const KIND: &'static str = "interlock";

    /// Builds the component from explicit points and the safe value, or
    /// reports a non-finite safe value as a [`ParameterError`].
    ///
    /// `trips` are the Boolean trip inputs, declared as `trip_1` …
    /// `trip_N` in the given order; pass an empty `Vec` for an interlock
    /// gated only by its permissive and the input's quality.
    pub fn new(
        name: impl Into<String>,
        input: PointId,
        permissive: PointId,
        trips: Vec<PointId>,
        output: PointId,
        tripped: PointId,
        safe_value: f64,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        if !safe_value.is_finite() {
            return Err(params::invalid(
                &name,
                "safe_value",
                "must be finite".to_string(),
            ));
        }
        Ok(Self {
            name,
            input,
            permissive,
            trips,
            output,
            tripped,
            safe_value,
        })
    }

    /// Builds the component from a plant-model parameter map, reading the
    /// parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        input: PointId,
        permissive: PointId,
        trips: Vec<PointId>,
        output: PointId,
        tripped: PointId,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let safe_value = params::required_f64(&name, parameters, "safe_value")?;
        Self::new(name, input, permissive, trips, output, tripped, safe_value)
    }
}

impl Component for Interlock {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        let mut requirements = Vec::with_capacity(self.trips.len() + 4);
        requirements.push(IoRequirement::input::<f64>("in", self.input));
        requirements.push(IoRequirement::input::<bool>("permissive", self.permissive));
        requirements.extend(self.trips.iter().enumerate().map(|(index, point)| {
            IoRequirement::input::<bool>(format!("trip_{}", index + 1), *point)
        }));
        requirements.push(IoRequirement::output::<f64>("out", self.output));
        requirements.push(IoRequirement::output::<bool>("tripped", self.tripped));
        requirements
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let input = io.read_typed::<f64>(self.input)?;
        let input_quality = if input.value.is_nan() {
            input
                .quality
                .merge(Quality::Bad(QualityReason::DeviceFault))
        } else {
            input.quality
        };
        let permissive = io.read_typed::<bool>(self.permissive)?;
        // Fail-safe evaluation: an input that is not `Good` cannot prove
        // its pass-through condition, whatever value it happens to read.
        let mut tripped =
            !input_quality.is_good() || !permissive.value || !permissive.quality.is_good();
        for trip in &self.trips {
            let sample = io.read_typed::<bool>(*trip)?;
            tripped = tripped || sample.value || !sample.quality.is_good();
        }
        let (out, quality) = if tripped {
            (self.safe_value, Quality::Good)
        } else {
            (input.value, input_quality)
        };
        io.write_sample(self.output, Sample::new(Value::Float(out), quality, tick))?;
        io.write_sample(
            self.tripped,
            Sample::new(Value::Bool(tripped), Quality::Good, tick),
        )?;
        Ok(())
    }

    /// Describes the interlock: `in` is the process value passed through,
    /// `permissive` and the `trip_N` inputs are the reported conditions
    /// gating it, `out` the driven value, `tripped` the reported state;
    /// the `safe_value` parameter.
    fn describe(&self) -> ComponentDescriptor {
        let mut roles: Vec<(String, PortRole)> = vec![
            ("in".to_string(), PortRole::ProcessValue),
            ("permissive".to_string(), PortRole::Status),
            ("out".to_string(), PortRole::Output),
            ("tripped".to_string(), PortRole::Status),
        ];
        roles.extend(
            (1..=self.trips.len()).map(|index| (format!("trip_{index}"), PortRole::Status)),
        );
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &roles,
            vec![describe::parameter(
                "safe_value",
                ValueKind::Float,
                Some(describe::FINITE_F64),
            )],
        )
    }

    /// Tunes `safe_value` at the scan boundary. The declared
    /// `FINITE_F64` bound already bars non-finite values; the hook
    /// re-checks so a caller bypassing the executor cannot install a
    /// `NaN` the fail-safe path would then drive.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "safe_value" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite",
                    ));
                }
                self.safe_value = tuned;
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }

    /// Captures the tuned `safe_value` — the interlock is otherwise
    /// stateless, but runtime tuning is run state a standby must
    /// inherit.
    fn capture_state(&self) -> StateMap {
        let mut state = StateMap::new();
        state.insert("safe_value", Value::Float(self.safe_value));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(&self.name, &["safe_value"])?;
        let safe_value = state.require_f64(&self.name, "safe_value")?;
        if !safe_value.is_finite() {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "safe_value".to_string(),
                value: Value::Float(safe_value),
            });
        }
        self.safe_value = safe_value;
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

    const IN: PointId = PointId(70);
    const PERMISSIVE: PointId = PointId(71);
    const TRIP_1: PointId = PointId(72);
    const TRIP_2: PointId = PointId(73);
    const OUT: PointId = PointId(74);
    const TRIPPED: PointId = PointId(75);

    const SAFE_VALUE: f64 = -1.0;

    /// A two-trip interlock with safe value -1.
    fn component() -> Interlock {
        Interlock::new(
            "ilk",
            IN,
            PERMISSIVE,
            vec![TRIP_1, TRIP_2],
            OUT,
            TRIPPED,
            SAFE_VALUE,
        )
        .unwrap()
    }

    /// I/O with all conditions satisfied: good input, asserted permissive,
    /// inactive trips.
    fn io() -> TestIo {
        TestIo::new(&[
            (
                IN,
                Direction::In,
                Sample::good(Value::Float(42.0), Tick::ZERO),
            ),
            (
                PERMISSIVE,
                Direction::In,
                Sample::good(Value::Bool(true), Tick::ZERO),
            ),
            (
                TRIP_1,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                TRIP_2,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                TRIPPED,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    fn output(io: &TestIo) -> Sample {
        io.written(OUT).unwrap()
    }

    fn tripped(io: &TestIo) -> bool {
        io.written(TRIPPED).unwrap().value == Value::Bool(true)
    }

    #[test]
    fn passes_input_through_while_conditions_hold() {
        let mut block = component();
        let io = io();
        block.step(&io, Tick(1)).unwrap();
        let out = output(&io);
        assert_eq!(out.value, Value::Float(42.0));
        assert_eq!(out.quality, Quality::Good);
        assert_eq!(out.tick, Tick(1));
        assert!(!tripped(&io));
    }

    #[test]
    fn each_configured_trip_drives_the_safe_value() {
        let mut block = component();
        let io = io();
        for trip in [TRIP_1, TRIP_2] {
            io.feed(trip, Sample::good(Value::Bool(true), Tick(1)));
            block.step(&io, Tick(1)).unwrap();
            let out = output(&io);
            assert_eq!(out.value, Value::Float(SAFE_VALUE), "trip={trip:?}");
            assert_eq!(out.quality, Quality::Good, "trip={trip:?}");
            assert!(tripped(&io), "trip={trip:?}");
            io.feed(trip, Sample::good(Value::Bool(false), Tick(2)));
            block.step(&io, Tick(2)).unwrap();
            assert_eq!(output(&io).value, Value::Float(42.0), "trip={trip:?}");
        }
    }

    #[test]
    fn unasserted_or_untrusted_permissive_drives_the_safe_value() {
        let mut block = component();
        let io = io();

        // A permissive that does not assert does not permit.
        io.feed(PERMISSIVE, Sample::good(Value::Bool(false), Tick(1)));
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(output(&io).value, Value::Float(SAFE_VALUE));
        assert!(tripped(&io));

        // Nor does a permissive whose quality is Bad.
        io.feed(
            PERMISSIVE,
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(2),
            ),
        );
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(output(&io).value, Value::Float(SAFE_VALUE));
        assert!(tripped(&io));

        // Only `Good` permits: an Uncertain permissive drives the safe
        // value too.
        io.feed(
            PERMISSIVE,
            Sample::new(
                Value::Bool(true),
                Quality::Uncertain(QualityReason::Stale),
                Tick(3),
            ),
        );
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(output(&io).value, Value::Float(SAFE_VALUE));
    }

    #[test]
    fn bad_input_quality_drives_the_safe_value() {
        let mut block = component();
        let io = io();
        for (tick, quality) in [
            Quality::Bad(QualityReason::DeviceFault),
            Quality::Uncertain(QualityReason::Stale),
        ]
        .into_iter()
        .enumerate()
        {
            io.feed(
                IN,
                Sample::new(Value::Float(42.0), quality, Tick(tick as u64 + 1)),
            );
            block.step(&io, Tick(tick as u64 + 1)).unwrap();
            assert_eq!(
                output(&io).value,
                Value::Float(SAFE_VALUE),
                "quality={quality:?}"
            );
            assert!(tripped(&io), "quality={quality:?}");
        }
    }

    #[test]
    fn nan_input_drives_the_safe_value() {
        let mut block = component();
        let io = io();
        io.feed(IN, Sample::good(Value::Float(f64::NAN), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(output(&io).value, Value::Float(SAFE_VALUE));
        assert!(tripped(&io));
    }

    #[test]
    fn untrusted_trip_reads_as_tripped() {
        let mut block = component();
        let io = io();
        // A Bad-quality trip reading false cannot prove it is inactive.
        io.feed(
            TRIP_1,
            Sample::new(
                Value::Bool(false),
                Quality::Bad(QualityReason::DeviceFault),
                Tick::ZERO,
            ),
        );
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(output(&io).value, Value::Float(SAFE_VALUE));
        assert!(tripped(&io));
    }

    #[test]
    fn auto_resets_to_pass_through_when_conditions_clear() {
        let mut block = component();
        let io = io();

        // Trip, then clear: pass-through resumes on the next scan —
        // the documented auto-reset rule holds no state.
        io.feed(TRIP_1, Sample::good(Value::Bool(true), Tick(1)));
        block.step(&io, Tick(1)).unwrap();
        assert!(tripped(&io));
        assert_eq!(output(&io).value, Value::Float(SAFE_VALUE));

        io.feed(TRIP_1, Sample::good(Value::Bool(false), Tick(2)));
        block.step(&io, Tick(2)).unwrap();
        assert!(!tripped(&io));
        assert_eq!(output(&io).value, Value::Float(42.0));
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [("safe_value".to_string(), Value::Int(-1))]
            .into_iter()
            .collect();
        let instance = ComponentInstance {
            id: ComponentId(7),
            kind: Interlock::KIND.to_string(),
            parameters,
            ports: BTreeMap::new(),
        };
        let mut block = Interlock::from_parameters(
            "ilk",
            IN,
            PERMISSIVE,
            vec![TRIP_1],
            OUT,
            TRIPPED,
            &instance.parameters,
        )
        .unwrap();
        assert_eq!(block.safe_value, SAFE_VALUE);

        let io = TestIo::new(&[
            (
                IN,
                Direction::In,
                Sample::good(Value::Float(5.0), Tick::ZERO),
            ),
            (
                PERMISSIVE,
                Direction::In,
                Sample::good(Value::Bool(true), Tick::ZERO),
            ),
            (
                TRIP_1,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                TRIPPED,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ]);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(output(&io).value, Value::Float(5.0));

        // safe_value is required and must be finite.
        assert!(matches!(
            Interlock::from_parameters(
                "ilk",
                IN,
                PERMISSIVE,
                vec![TRIP_1],
                OUT,
                TRIPPED,
                &Parameters::new(),
            )
            .unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "safe_value"
        ));
        let non_finite: Parameters = [("safe_value".to_string(), Value::Float(f64::INFINITY))]
            .into_iter()
            .collect();
        assert!(matches!(
            Interlock::from_parameters(
                "ilk",
                IN,
                PERMISSIVE,
                vec![TRIP_1],
                OUT,
                TRIPPED,
                &non_finite,
            )
            .unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "safe_value"
        ));
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "ilk");
        assert_eq!(descriptor.kind, Interlock::KIND);
        assert_eq!(descriptor.label, "ilk");
        // Ports follow the declared-I/O order: in, permissive, the
        // trip_N set, out, tripped.
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "in".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                },
                PortDescriptor {
                    name: "permissive".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                },
                PortDescriptor {
                    name: "trip_1".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                },
                PortDescriptor {
                    name: "trip_2".to_string(),
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
                PortDescriptor {
                    name: "tripped".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                },
            ]
        );
        // Drift guard: the descriptor's parameter names are exactly the
        // keys `from_parameters` reads.
        assert_eq!(
            descriptor.parameters,
            [ParameterDescriptor {
                name: "safe_value".to_string(),
                kind: ValueKind::Float,
                range: Some(describe::FINITE_F64),
            }]
        );

        // A trip-free instance describes no trip ports.
        let solo = Interlock::new("ilk-0", IN, PERMISSIVE, Vec::new(), OUT, TRIPPED, 0.0).unwrap();
        assert_eq!(solo.describe().ports.len(), 4);
    }
}
