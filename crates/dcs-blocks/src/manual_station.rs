//! Manual/auto station: operator-selectable source on an analog output,
//! with a slew-bounded bumpless transfer between sources.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, PointId, PortRole, Quality, QualityReason, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// A manual/auto station: the Boolean `mode` selects which of the two
/// analog inputs `out` follows — `control` (automatic) while `mode`
/// reads `false`, `manual` (the operator's entered value) while `mode`
/// reads `true`. `manual_active` reports the selection.
///
/// **Bumpless transfer** — the output slews to the new source: while
/// `mode` holds its value between scans, `out` passes the selected
/// input through directly. On the scan `mode` changes, the station
/// begins a transfer instead of jumping: `out` moves from its current
/// value toward the newly selected input by at most `transfer_delta`
/// per scan until it arrives, then resumes pass-through. A mode change
/// mid-transfer retargets the slew from wherever `out` has reached, and
/// a selected source already within `transfer_delta` arrives on the
/// switch scan itself. The first finite selected value is adopted
/// outright — initialization is not a transfer.
///
/// **Quality rule:** `out` carries the worst of all three inputs'
/// qualities — control, manual, and mode merged — so a degraded input
/// marks the output even while it is the unselected one, and an
/// untrusted `mode` marks the output whose source it chose. A
/// non-finite selected input cannot be slewed to or passed through:
/// `out` holds its last value, merges `Bad(DeviceFault)`, and — before
/// any finite input has been seen — passes the value through, surfacing
/// the fault rather than inventing a number. `manual_active` asserts on
/// the switch scan, ahead of the transfer completing, and is stamped
/// with `mode`'s own quality.
///
/// Declared I/O: `control` (`In`, `Float`), `manual` (`In`, `Float`),
/// `mode` (`In`, `Bool`), `out` (`Out`, `Float`), `manual_active`
/// (`Out`, `Bool`).
///
/// Parameters: `transfer_delta` — required finite `Float` (or
/// losslessly representable `Int`), strictly positive: the per-scan
/// slew bound a mode switch engages.
#[derive(Debug)]
pub struct ManualStation {
    name: String,
    control: PointId,
    manual: PointId,
    mode: PointId,
    output: PointId,
    status: PointId,
    transfer_delta: f64,
    /// The value `out` currently drives; `None` until the first finite
    /// selected input is adopted.
    current: Option<f64>,
    /// Whether a mode switch is mid-slew — `out` has not yet reached
    /// the selected source.
    transferring: bool,
    /// The mode observed on the previous scan, for edge detection;
    /// `None` until the first step.
    last_mode: Option<bool>,
}

impl ManualStation {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "manual-station";

    /// Builds the component from explicit points and the per-scan slew
    /// bound a mode switch engages.
    ///
    /// `transfer_delta` must be finite and strictly positive;
    /// violations are reported as a [`ParameterError`].
    pub fn new(
        name: impl Into<String>,
        control: PointId,
        manual: PointId,
        mode: PointId,
        output: PointId,
        status: PointId,
        transfer_delta: f64,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        if !transfer_delta.is_finite() {
            return Err(params::invalid(
                &name,
                "transfer_delta",
                "must be finite".to_string(),
            ));
        }
        if transfer_delta <= 0.0 {
            return Err(params::invalid(
                &name,
                "transfer_delta",
                "must be positive".to_string(),
            ));
        }
        Ok(Self {
            name,
            control,
            manual,
            mode,
            output,
            status,
            transfer_delta,
            current: None,
            transferring: false,
            last_mode: None,
        })
    }

    /// Builds the component from a plant-model parameter map, reading
    /// the parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        control: PointId,
        manual: PointId,
        mode: PointId,
        output: PointId,
        status: PointId,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let transfer_delta = params::required_f64(&name, parameters, "transfer_delta")?;
        Self::new(name, control, manual, mode, output, status, transfer_delta)
    }
}

impl Component for ManualStation {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("control", self.control),
            IoRequirement::input::<f64>("manual", self.manual),
            IoRequirement::input::<bool>("mode", self.mode),
            IoRequirement::output::<f64>("out", self.output),
            IoRequirement::output::<bool>("manual_active", self.status),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let control = io.read_typed::<f64>(self.control)?;
        let manual = io.read_typed::<f64>(self.manual)?;
        let mode = io.read_typed::<bool>(self.mode)?;
        let selected = if mode.value { manual } else { control };

        let mut quality = control.quality.merge(manual.quality).merge(mode.quality);
        if !selected.value.is_finite() {
            quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
        }

        // The scan `mode` changes on starts the transfer; edge
        // detection on the value runs whatever the mode's quality.
        if self.last_mode.is_some_and(|last| last != mode.value) {
            self.transferring = true;
        }
        self.last_mode = Some(mode.value);

        let next = if !selected.value.is_finite() {
            // A non-finite source can be neither approached nor passed:
            // hold the driven value — or surface the input before the
            // first finite one.
            self.current.unwrap_or(selected.value)
        } else {
            let next = match self.current {
                None => {
                    // The first finite input initializes; the bound
                    // does not apply to it.
                    self.transferring = false;
                    selected.value
                }
                Some(current) if self.transferring => {
                    let gap = selected.value - current;
                    let next = if gap.abs() <= self.transfer_delta {
                        selected.value
                    } else {
                        current + gap.signum() * self.transfer_delta
                    };
                    if next == current || next == selected.value {
                        // Arrived — inside the bound, or the step
                        // landed below the value's precision, where a
                        // slew could never finish.
                        self.transferring = false;
                        selected.value
                    } else {
                        next
                    }
                }
                Some(_) => selected.value,
            };
            self.current = Some(next);
            next
        };

        io.write_sample(self.output, Sample::new(Value::Float(next), quality, tick))?;
        io.write_sample(
            self.status,
            Sample::new(Value::Bool(mode.value), mode.quality, tick),
        )?;
        Ok(())
    }

    /// Describes the station: `control` is the process-side value,
    /// `manual` the operator's target, `mode` the reported selection
    /// choosing between them, `out` the driven value, `manual_active`
    /// the reported mode; the `transfer_delta` parameter
    /// `from_parameters` reads.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[
                ("control", PortRole::ProcessValue),
                ("manual", PortRole::Setpoint),
                ("mode", PortRole::Status),
                ("out", PortRole::Output),
                ("manual_active", PortRole::Status),
            ],
            vec![describe::parameter(
                "transfer_delta",
                ValueKind::Float,
                Some(describe::POSITIVE_F64),
            )],
        )
    }

    /// Tunes `transfer_delta` at the scan boundary; the new bound
    /// applies to any slew in progress from the next scan on.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "transfer_delta" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() || tuned <= 0.0 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite and positive",
                    ));
                }
                self.transfer_delta = tuned;
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }

    /// Captures the value `out` is driving — absent before the first
    /// finite input — the in-progress transfer flag, the last observed
    /// mode for edge detection, and the tuned `transfer_delta`, so a
    /// checkpointed station continues a mid-slew transfer under the
    /// same bound.
    fn capture_state(&self) -> StateMap {
        let mut state = StateMap::new();
        if let Some(current) = self.current {
            state.insert("current", Value::Float(current));
        }
        state.insert("transferring", Value::Bool(self.transferring));
        if let Some(mode) = self.last_mode {
            state.insert("last_mode", Value::Bool(mode));
        }
        state.insert("transfer_delta", Value::Float(self.transfer_delta));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(
            &self.name,
            &["current", "transferring", "last_mode", "transfer_delta"],
        )?;
        let current = match state.optional_f64(&self.name, "current")? {
            Some(current) if current.is_finite() => Some(current),
            Some(current) => {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: "current".to_string(),
                    value: Value::Float(current),
                });
            }
            None => None,
        };
        let transfer_delta = state.require_f64(&self.name, "transfer_delta")?;
        if !transfer_delta.is_finite() || transfer_delta <= 0.0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "transfer_delta".to_string(),
                value: Value::Float(transfer_delta),
            });
        }
        self.current = current;
        self.transferring = state.require_bool(&self.name, "transferring")?;
        self.last_mode = state.optional_bool(&self.name, "last_mode")?;
        self.transfer_delta = transfer_delta;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, ParameterDescriptor, PortDescriptor};
    use dcs_model::{ComponentId, ComponentInstance};
    use std::collections::BTreeMap;

    const CONTROL: PointId = PointId(140);
    const MANUAL: PointId = PointId(141);
    const MODE: PointId = PointId(142);
    const OUT: PointId = PointId(143);
    const ACTIVE: PointId = PointId(144);

    /// A station slewing at most 5 units per scan on a mode switch.
    fn component() -> ManualStation {
        ManualStation::new("mas", CONTROL, MANUAL, MODE, OUT, ACTIVE, 5.0).unwrap()
    }

    fn io(mode: bool) -> TestIo {
        TestIo::new(&[
            (
                CONTROL,
                Direction::In,
                Sample::good(Value::Float(10.0), Tick::ZERO),
            ),
            (
                MANUAL,
                Direction::In,
                Sample::good(Value::Float(30.0), Tick::ZERO),
            ),
            (
                MODE,
                Direction::In,
                Sample::good(Value::Bool(mode), Tick::ZERO),
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

    fn feed(io: &TestIo, point: PointId, value: Value, tick: u64) {
        io.feed(point, Sample::good(value, Tick(tick)));
    }

    fn driven(io: &TestIo) -> f64 {
        match io.written(OUT).unwrap().value {
            Value::Float(value) => value,
            other => panic!("out must be Float, found {other:?}"),
        }
    }

    fn active(io: &TestIo) -> Sample {
        io.written(ACTIVE).unwrap()
    }

    #[test]
    fn auto_mode_passes_control_and_reports_the_mode() {
        let mut block = component();
        let io = io(false);
        block.step(&io, Tick(1)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Float(10.0));
        assert_eq!(out.quality, Quality::Good);
        assert_eq!(out.tick, Tick(1));
        assert_eq!(active(&io).value, Value::Bool(false));
        assert_eq!(active(&io).quality, Quality::Good);
    }

    #[test]
    fn manual_mode_passes_the_operator_value() {
        let mut block = component();
        // Start in manual: the first finite selected value initializes.
        let io = io(true);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(driven(&io), 30.0);
        assert_eq!(active(&io).value, Value::Bool(true));
    }

    #[test]
    fn a_mode_switch_slews_to_the_new_source_by_transfer_delta() {
        let mut block = component();
        let io = io(false);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(driven(&io), 10.0);

        // The switch scan begins the slew — 10 + 5 — and asserts the
        // mode status already.
        feed(&io, MODE, Value::Bool(true), 2);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(driven(&io), 15.0);
        assert_eq!(active(&io).value, Value::Bool(true));

        // The bound holds per scan until the output arrives.
        for (tick, expected) in [(3, 20.0), (4, 25.0), (5, 30.0)] {
            feed(&io, MODE, Value::Bool(true), tick);
            block.step(&io, Tick(tick)).unwrap();
            assert_eq!(driven(&io), expected, "tick={tick}");
        }

        // Arrived: the manual source passes through, unbounded.
        feed(&io, MANUAL, Value::Float(33.0), 6);
        block.step(&io, Tick(6)).unwrap();
        assert_eq!(driven(&io), 33.0);
    }

    #[test]
    fn a_source_inside_the_bound_arrives_on_the_switch_scan() {
        let mut block = component();
        let io = io(false);
        block.step(&io, Tick(1)).unwrap();
        // Manual sits 3 units away — inside the 5-unit bound.
        feed(&io, MANUAL, Value::Float(13.0), 2);
        feed(&io, MODE, Value::Bool(true), 2);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(driven(&io), 13.0);
        // And manual mode passes through from there.
        feed(&io, MANUAL, Value::Float(40.0), 3);
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(driven(&io), 40.0);
    }

    #[test]
    fn a_mode_change_mid_transfer_retargets_the_slew() {
        let mut block = component();
        let io = io(false);
        block.step(&io, Tick(1)).unwrap();

        // Up toward manual=30, one step in.
        feed(&io, MODE, Value::Bool(true), 2);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(driven(&io), 15.0);

        // Back to auto before arriving: the slew retargets control=10
        // from wherever the output reached.
        feed(&io, MODE, Value::Bool(false), 3);
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(driven(&io), 10.0);
        assert_eq!(active(&io).value, Value::Bool(false));
    }

    #[test]
    fn a_moving_target_is_followed_through_the_transfer() {
        let mut block = component();
        let io = io(false);
        block.step(&io, Tick(1)).unwrap();
        feed(&io, MODE, Value::Bool(true), 2);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(driven(&io), 15.0);

        // The manual value moves away mid-transfer: the slew follows
        // it, still bounded.
        feed(&io, MANUAL, Value::Float(50.0), 3);
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(driven(&io), 20.0);
    }

    #[test]
    fn reports_worst_of_input_quality() {
        let mut block = component();
        // A Bad unselected input marks the output.
        let io = io(false);
        io.feed(
            MANUAL,
            Sample::new(
                Value::Float(30.0),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(1),
            ),
        );
        block.step(&io, Tick(1)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Float(10.0));
        assert_eq!(out.quality, Quality::Bad(QualityReason::DeviceFault));

        // An Uncertain select marks the output whose source it chose.
        io.feed(MANUAL, Sample::good(Value::Float(30.0), Tick(2)));
        io.feed(
            MODE,
            Sample::new(
                Value::Bool(false),
                Quality::Uncertain(QualityReason::Stale),
                Tick(2),
            ),
        );
        block.step(&io, Tick(2)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Float(10.0));
        assert_eq!(out.quality, Quality::Uncertain(QualityReason::Stale));
        // `manual_active` carries mode's own quality.
        assert_eq!(
            active(&io).quality,
            Quality::Uncertain(QualityReason::Stale)
        );
    }

    #[test]
    fn non_finite_selected_input_holds_and_marks_bad() {
        let mut block = component();
        let io = io(false);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(driven(&io), 10.0);

        feed(&io, CONTROL, Value::Float(f64::NAN), 2);
        block.step(&io, Tick(2)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Float(10.0));
        assert_eq!(out.quality, Quality::Bad(QualityReason::DeviceFault));

        // Recovery resumes pass-through; no transfer was engaged.
        feed(&io, CONTROL, Value::Float(14.0), 3);
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(driven(&io), 14.0);
    }

    #[test]
    fn non_finite_first_input_passes_through_flagged() {
        let mut block = component();
        let io = io(false);
        feed(&io, CONTROL, Value::Float(f64::INFINITY), 1);
        block.step(&io, Tick(1)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Float(f64::INFINITY));
        assert_eq!(out.quality, Quality::Bad(QualityReason::DeviceFault));
        // Nothing was adopted: the first finite input still initializes.
        feed(&io, CONTROL, Value::Float(10.0), 2);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(driven(&io), 10.0);
    }

    #[test]
    fn non_finite_target_mid_transfer_holds_then_resumes() {
        let mut block = component();
        let io = io(false);
        block.step(&io, Tick(1)).unwrap();
        feed(&io, MODE, Value::Bool(true), 2);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(driven(&io), 15.0);

        // The selected source goes non-finite mid-transfer: the output
        // holds and the transfer stays engaged.
        feed(&io, MANUAL, Value::Float(f64::NAN), 3);
        block.step(&io, Tick(3)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Float(15.0));
        assert_eq!(out.quality, Quality::Bad(QualityReason::DeviceFault));

        feed(&io, MANUAL, Value::Float(30.0), 4);
        block.step(&io, Tick(4)).unwrap();
        assert_eq!(driven(&io), 20.0);
    }

    #[test]
    fn restored_station_continues_mid_transfer() {
        let mut block = component();
        let io = io(false);
        block.step(&io, Tick(1)).unwrap();
        feed(&io, MODE, Value::Bool(true), 2);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(driven(&io), 15.0);

        // Checkpoint mid-slew: driven value, engaged transfer, and the
        // observed mode all ride along.
        let state = block.capture_state();
        assert_eq!(state.get("current"), Some(Value::Float(15.0)));
        assert_eq!(state.get("transferring"), Some(Value::Bool(true)));
        assert_eq!(state.get("last_mode"), Some(Value::Bool(true)));

        let mut standby = component();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);
        feed(&io, MODE, Value::Bool(true), 3);
        standby.step(&io, Tick(3)).unwrap();
        assert_eq!(driven(&io), 20.0);

        // An uninitialized capture restores an uninitialized station:
        // no edge on the first scan and the first finite input
        // initializes.
        let state = component().capture_state();
        assert_eq!(state.get("current"), None);
        assert_eq!(state.get("last_mode"), None);
        let mut fresh = component();
        fresh.restore_state(&state).unwrap();
        feed(&io, MODE, Value::Bool(false), 4);
        fresh.step(&io, Tick(4)).unwrap();
        assert_eq!(driven(&io), 10.0);
    }

    #[test]
    fn incompatible_state_is_a_named_error() {
        let mut block = component();
        let mut state = StateMap::new();
        state.insert("current", Value::Float(f64::NAN));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "current"
        ));
        let mut state = StateMap::new();
        state.insert("transferring", Value::Int(1));
        state.insert("transfer_delta", Value::Float(5.0));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::IncompatibleField { ref field, .. }) if field == "transferring"
        ));
        let mut state = StateMap::new();
        state.insert("other", Value::Float(1.0));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::UnknownField { ref field, .. }) if field == "other"
        ));
        // `transferring` and `transfer_delta` are required.
        assert!(matches!(
            block.restore_state(&StateMap::new()),
            Err(StateError::MissingField { ref field, .. }) if field == "transfer_delta"
        ));
    }

    #[test]
    fn apply_parameter_retunes_the_slew_bound() {
        let mut block = component();
        block
            .apply_parameter("transfer_delta", Value::Float(1.0))
            .unwrap();
        assert_eq!(block.transfer_delta, 1.0);
        assert_eq!(
            block.capture_state().get("transfer_delta"),
            Some(Value::Float(1.0))
        );

        assert_eq!(
            block
                .apply_parameter("other", Value::Float(1.0))
                .unwrap_err(),
            CommandError::UnknownParameter {
                component: "mas".to_string(),
                parameter: "other".to_string(),
            }
        );
        assert!(matches!(
            block.apply_parameter("transfer_delta", Value::Bool(true)).unwrap_err(),
            CommandError::ParameterTypeMismatch { ref parameter, .. } if parameter == "transfer_delta"
        ));
        assert!(matches!(
            block.apply_parameter("transfer_delta", Value::Float(0.0)).unwrap_err(),
            CommandError::InvalidParameter { ref parameter, .. } if parameter == "transfer_delta"
        ));
        assert_eq!(block.transfer_delta, 1.0);
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [("transfer_delta".to_string(), Value::Float(5.0))]
            .into_iter()
            .collect();
        let instance = ComponentInstance {
            id: ComponentId(12),
            kind: ManualStation::KIND.to_string(),
            parameters,
            ports: BTreeMap::new(),
        };
        let block = ManualStation::from_parameters(
            "mas",
            CONTROL,
            MANUAL,
            MODE,
            OUT,
            ACTIVE,
            &instance.parameters,
        )
        .unwrap();
        assert_eq!(block.transfer_delta, 5.0);

        assert!(matches!(
            ManualStation::from_parameters(
                "mas",
                CONTROL,
                MANUAL,
                MODE,
                OUT,
                ACTIVE,
                &Parameters::new(),
            )
            .unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "transfer_delta"
        ));
        for bad in [
            Value::Float(0.0),
            Value::Float(-1.0),
            Value::Float(f64::NAN),
            Value::Bool(true),
        ] {
            let parameters: Parameters =
                [("transfer_delta".to_string(), bad)].into_iter().collect();
            assert!(
                matches!(
                    ManualStation::from_parameters(
                        "mas", CONTROL, MANUAL, MODE, OUT, ACTIVE, &parameters,
                    )
                    .unwrap_err(),
                    ParameterError::Invalid { ref parameter, .. } if parameter == "transfer_delta"
                ),
                "transfer_delta={bad:?}"
            );
        }
        // A losslessly representable Int is accepted.
        let parameters: Parameters = [("transfer_delta".to_string(), Value::Int(2))]
            .into_iter()
            .collect();
        let block =
            ManualStation::from_parameters("mas", CONTROL, MANUAL, MODE, OUT, ACTIVE, &parameters)
                .unwrap();
        assert_eq!(block.transfer_delta, 2.0);
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "mas");
        assert_eq!(descriptor.kind, ManualStation::KIND);
        assert_eq!(descriptor.label, "mas");
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
                    name: "manual".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Setpoint),
                },
                PortDescriptor {
                    name: "mode".to_string(),
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
                    name: "manual_active".to_string(),
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
                name: "transfer_delta".to_string(),
                kind: ValueKind::Float,
                range: Some(describe::POSITIVE_F64),
            }]
        );
    }
}
