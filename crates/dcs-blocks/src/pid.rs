//! PID controller: parallel form, output limits, and conditional-
//! integration anti-windup.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    ComponentDescriptor, PointId, PortRole, Quality, QualityReason, Sample, StateError, StateMap,
    Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// Tuning and limit parameters for [`Pid`].
///
/// The controller uses the parallel (non-interacting) form: each gain is
/// independent, and the output before limiting is
///
/// ```text
/// u = kp·e + i + d          e = sp - pv
/// i += ki·e·dt              per scan, subject to anti-windup
/// d  = -kd·(pv - pv_prev)/dt   derivative on measurement
/// ```
///
/// `dt` is the scan period in the process's time units — the value the
/// simulated backend steps by — so `ki` has units of 1/time and `kd` of
/// time. The derivative acts on the measurement, not the error, so a
/// setpoint step does not produce a derivative kick. All fields must be
/// finite, `dt` must be positive, and `out_min < out_max`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct PidConfig {
    /// Proportional gain: output units per unit of `sp - pv` error.
    pub kp: f64,
    /// Integral gain in 1/time: the integrator accumulates `ki·e·dt` per
    /// scan. Zero disables the integral term.
    pub ki: f64,
    /// Derivative gain in time units, applied to the measurement's rate of
    /// change. Zero disables the derivative term.
    pub kd: f64,
    /// The scan period the gains are tuned for, in the process's time
    /// units. Must be finite and positive.
    pub dt: f64,
    /// Lower output limit. Must be finite and below `out_max`.
    pub out_min: f64,
    /// Upper output limit. Must be finite and above `out_min`.
    pub out_max: f64,
}

/// A PID controller: reads setpoint `sp` and process variable `pv`, writes
/// the manipulated variable `out`.
///
/// Anti-windup is *conditional integration* (clamping): each scan first
/// forms the candidate integral `i + ki·e·dt` and the unlimited output. If
/// the output saturates **and** the error would push it further into the
/// same limit, the candidate is discarded — the integrator freezes at its
/// last value instead of accumulating. While saturated against the error,
/// the integrator therefore cannot grow; as soon as the error reverses
/// sign, integration resumes and pulls the output back inside the limits.
///
/// While either input is not [`Quality::Good`] or not finite, the
/// controller holds its last output and internal state (integrator and
/// previous `pv`) and stamps the merged input quality on the output —
/// non-finite inputs additionally mark the output `Bad(DeviceFault)`.
///
/// Declared I/O: `sp` (`In`, `Float`), `pv` (`In`, `Float`), `out`
/// (`Out`, `Float`).
///
/// Parameters: `kp`, `dt`, `out_min`, `out_max` required; `ki` and `kd`
/// optional, defaulting to `0.0` (a P-only or PD controller). All values
/// are finite `Float`s or losslessly representable `Int`s.
#[derive(Debug)]
pub struct Pid {
    name: String,
    setpoint: PointId,
    process: PointId,
    output: PointId,
    config: PidConfig,
    /// The committed integral term; see the type docs for anti-windup.
    integrator: f64,
    /// The previous scan's `pv`, for derivative on measurement.
    previous_pv: Option<f64>,
    /// The last output written; held while inputs are unusable.
    last_output: f64,
}

impl Pid {
    /// The component-kind string the model-driven registry maps onto this
    /// type's constructor.
    pub const KIND: &'static str = "pid";

    /// Builds the component from explicit points and tuning, or reports
    /// the config's inconsistency as a [`ParameterError`].
    pub fn new(
        name: impl Into<String>,
        setpoint: PointId,
        process: PointId,
        output: PointId,
        config: PidConfig,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        for (parameter, value) in [
            ("kp", config.kp),
            ("ki", config.ki),
            ("kd", config.kd),
            ("dt", config.dt),
            ("out_min", config.out_min),
            ("out_max", config.out_max),
        ] {
            if !value.is_finite() {
                return Err(params::invalid(
                    &name,
                    parameter,
                    "must be finite".to_string(),
                ));
            }
        }
        if config.dt <= 0.0 {
            return Err(params::invalid(&name, "dt", "must be positive".to_string()));
        }
        if config.out_min >= config.out_max {
            return Err(params::invalid(
                &name,
                "out_max",
                "output limits require out_min < out_max".to_string(),
            ));
        }
        Ok(Self {
            name,
            setpoint,
            process,
            output,
            config,
            integrator: 0.0,
            previous_pv: None,
            // Until the first controlled step, hold the neutral output.
            last_output: 0.0_f64.clamp(config.out_min, config.out_max),
        })
    }

    /// Builds the component from a plant-model parameter map, reading the
    /// parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        setpoint: PointId,
        process: PointId,
        output: PointId,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let config = PidConfig {
            kp: params::required_f64(&name, parameters, "kp")?,
            ki: params::optional_f64(&name, parameters, "ki")?.unwrap_or(0.0),
            kd: params::optional_f64(&name, parameters, "kd")?.unwrap_or(0.0),
            dt: params::required_f64(&name, parameters, "dt")?,
            out_min: params::required_f64(&name, parameters, "out_min")?,
            out_max: params::required_f64(&name, parameters, "out_max")?,
        };
        Self::new(name, setpoint, process, output, config)
    }

    /// The committed integral term, exposed for diagnostics and tests.
    pub fn integral(&self) -> f64 {
        self.integrator
    }

    /// The output the controller last wrote (or holds initially).
    pub fn last_output(&self) -> f64 {
        self.last_output
    }
}

impl Component for Pid {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("sp", self.setpoint),
            IoRequirement::input::<f64>("pv", self.process),
            IoRequirement::output::<f64>("out", self.output),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let sp = io.read_typed::<f64>(self.setpoint)?;
        let pv = io.read_typed::<f64>(self.process)?;

        let mut quality = sp.quality.merge(pv.quality);
        if !sp.value.is_finite() || !pv.value.is_finite() {
            quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
        }
        if !quality.is_good() {
            // Hold the last output and all state; propagate the quality.
            io.write_sample(
                self.output,
                Sample::new(Value::Float(self.last_output), quality, tick),
            )?;
            return Ok(());
        }

        let error = sp.value - pv.value;
        let derivative = match self.previous_pv {
            Some(previous) => -self.config.kd * (pv.value - previous) / self.config.dt,
            None => 0.0,
        };
        self.previous_pv = Some(pv.value);

        let candidate = self.integrator + self.config.ki * error * self.config.dt;
        let unclamped = self.config.kp * error + candidate + derivative;
        let output = unclamped.clamp(self.config.out_min, self.config.out_max);

        // Conditional integration: refuse the integral update only while
        // the output saturates in the direction the error pushes it.
        let winding_up = (unclamped > self.config.out_max && error > 0.0)
            || (unclamped < self.config.out_min && error < 0.0);
        if !winding_up {
            self.integrator = candidate;
        }

        self.last_output = output;
        io.write_typed(self.output, output)?;
        Ok(())
    }

    /// Describes the controller: `sp` the setpoint it regulates toward,
    /// `pv` the measured process value, `out` the manipulated variable it
    /// drives; the tuning and limit parameters `from_parameters` reads.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[
                ("sp", PortRole::Setpoint),
                ("pv", PortRole::ProcessValue),
                ("out", PortRole::Output),
            ],
            vec![
                describe::parameter("kp", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("ki", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("kd", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("dt", ValueKind::Float, Some(describe::POSITIVE_F64)),
                describe::parameter("out_min", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("out_max", ValueKind::Float, Some(describe::FINITE_F64)),
            ],
        )
    }

    /// Captures the integrator, the previous `pv` (when any step has run),
    /// and the held last output — the state a standby needs to continue
    /// the loop bumplessly.
    fn capture_state(&self) -> StateMap {
        let mut state = StateMap::new();
        state.insert("integrator", Value::Float(self.integrator));
        state.insert("last_output", Value::Float(self.last_output));
        if let Some(previous) = self.previous_pv {
            state.insert("previous_pv", Value::Float(previous));
        }
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(&self.name, &["integrator", "last_output", "previous_pv"])?;
        let integrator = state.require_f64(&self.name, "integrator")?;
        let last_output = state.require_f64(&self.name, "last_output")?;
        let previous_pv = state.optional_f64(&self.name, "previous_pv")?;
        self.integrator = integrator;
        self.last_output = last_output;
        self.previous_pv = previous_pv;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, IoDriver, ParameterDescriptor, PortDescriptor, Quality};
    use dcs_model::{ComponentId, ComponentInstance};
    use dcs_runtime::Executor;
    use dcs_sim::{ChannelId, ChannelMap, FirstOrderLag, PointBinding, ProcessElement, SimDriver};
    use std::collections::BTreeMap;

    const SP: PointId = PointId(11);
    const PV: PointId = PointId(10);
    const OUT: PointId = PointId(20);

    fn config() -> PidConfig {
        PidConfig {
            kp: 1.0,
            ki: 1.0,
            kd: 0.0,
            dt: 0.1,
            out_min: 0.0,
            out_max: 5.0,
        }
    }

    /// A `TestIo` with `sp`/`pv` inputs and the `out` output.
    fn io(sp: f64, pv: f64) -> TestIo {
        TestIo::new(&[
            (
                SP,
                Direction::In,
                Sample::good(Value::Float(sp), Tick::ZERO),
            ),
            (
                PV,
                Direction::In,
                Sample::good(Value::Float(pv), Tick::ZERO),
            ),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
        ])
    }

    #[test]
    fn integrator_accumulates_while_unsaturated() {
        let mut pid = Pid::new(
            "pid",
            SP,
            PV,
            OUT,
            PidConfig {
                kp: 0.0,
                out_min: -100.0,
                out_max: 100.0,
                ..config()
            },
        )
        .unwrap();
        // Constant unit error: i += ki·e·dt = 0.1 per scan.
        let io = io(1.0, 0.0);
        for tick in 1..=10 {
            pid.step(&io, Tick(tick)).unwrap();
        }
        assert!((pid.integral() - 1.0).abs() < 1e-12, "{}", pid.integral());
        let Value::Float(u) = io.written(OUT).unwrap().value else {
            panic!("out must be Float")
        };
        assert!((u - 1.0).abs() < 1e-12, "u={u}");
    }

    #[test]
    fn anti_windup_freezes_integrator_while_saturated() {
        let mut pid = Pid::new("pid", SP, PV, OUT, config()).unwrap();
        // kp·e = 10 exceeds out_max=5: the output saturates against a
        // positive error, so the integral candidate is never committed.
        let io = io(10.0, 0.0);
        for tick in 1..=50 {
            pid.step(&io, Tick(tick)).unwrap();
        }
        assert_eq!(pid.integral(), 0.0);
        assert_eq!(io.written(OUT).unwrap().value, Value::Float(5.0));

        // Once the error shrinks enough that the output leaves the limit,
        // integration resumes: i = 0 + ki·(0.5)·dt = 0.05.
        io.feed(PV, Sample::good(Value::Float(9.5), Tick(51)));
        pid.step(&io, Tick(51)).unwrap();
        assert!((pid.integral() - 0.05).abs() < 1e-12, "{}", pid.integral());
        let Value::Float(u) = io.written(OUT).unwrap().value else {
            panic!("out must be Float")
        };
        assert!((u - 0.55).abs() < 1e-12, "u={u}");
    }

    #[test]
    fn non_good_input_holds_output_and_state() {
        let mut pid = Pid::new("pid", SP, PV, OUT, config()).unwrap();
        let io = io(1.0, 0.0);
        pid.step(&io, Tick(1)).unwrap();
        let held = pid.integral();
        let held_output = pid.last_output();
        assert!(held > 0.0 && held_output > 0.0);

        // A bad pv freezes the controller and marks the output bad.
        io.feed(
            PV,
            Sample::new(
                Value::Float(0.0),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(2),
            ),
        );
        pid.step(&io, Tick(2)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Float(held_output));
        assert_eq!(out.quality, Quality::Bad(QualityReason::CommunicationFault));
        assert_eq!(pid.integral(), held);
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [
            ("kp".to_string(), Value::Float(2.0)),
            ("dt".to_string(), Value::Float(0.1)),
            ("out_min".to_string(), Value::Int(-5)),
            ("out_max".to_string(), Value::Int(5)),
        ]
        .into_iter()
        .collect();
        let instance = ComponentInstance {
            id: ComponentId(2),
            kind: Pid::KIND.to_string(),
            parameters,
            ports: BTreeMap::new(),
        };
        let pid = Pid::from_parameters("pid", SP, PV, OUT, &instance.parameters).unwrap();
        // Optional ki/kd default to zero.
        assert_eq!(pid.config.ki, 0.0);
        assert_eq!(pid.config.kd, 0.0);
        assert_eq!(pid.config.kp, 2.0);

        assert!(matches!(
            Pid::from_parameters("pid", SP, PV, OUT, &Parameters::new()).unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "kp"
        ));
    }

    #[test]
    fn invalid_config_is_rejected() {
        let bad_limits = PidConfig {
            out_min: 5.0,
            out_max: 5.0,
            ..config()
        };
        assert!(matches!(
            Pid::new("pid", SP, PV, OUT, bad_limits),
            Err(ParameterError::Invalid { .. })
        ));
        let bad_dt = PidConfig {
            dt: 0.0,
            ..config()
        };
        assert!(matches!(
            Pid::new("pid", SP, PV, OUT, bad_dt),
            Err(ParameterError::Invalid { .. })
        ));
    }

    #[test]
    fn describes_itself() {
        let descriptor = Pid::new("pid", SP, PV, OUT, config()).unwrap().describe();
        assert_eq!(descriptor.name, "pid");
        assert_eq!(descriptor.kind, Pid::KIND);
        assert_eq!(descriptor.label, "pid");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "sp".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Setpoint),
                },
                PortDescriptor {
                    name: "pv".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                },
                PortDescriptor {
                    name: "out".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Output),
                },
            ]
        );
        // Drift guard: the descriptor's parameter names are exactly the
        // keys `from_parameters` reads.
        assert_eq!(
            descriptor.parameters,
            [
                ParameterDescriptor {
                    name: "kp".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "ki".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "kd".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "dt".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::POSITIVE_F64),
                },
                ParameterDescriptor {
                    name: "out_min".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "out_max".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
            ]
        );
    }

    fn sim_point(point: PointId, direction: dcs_sim::Direction) -> PointBinding {
        PointBinding {
            point,
            channel: ChannelId {
                device: 1,
                name: format!("ch{}", point.0),
            },
            direction,
            initial: Value::Float(0.0),
        }
    }

    #[test]
    fn drives_first_order_process_to_setpoint() {
        // Closed loop: the executor reads pv/sp and writes u; the sim's
        // first-order lag (tau = 1) reads u and drives pv.
        let channel_map = ChannelMap::new()
            .with_point(sim_point(PV, dcs_sim::Direction::In))
            .with_point(sim_point(SP, dcs_sim::Direction::In))
            .with_point(sim_point(OUT, dcs_sim::Direction::Out))
            .with_element(ProcessElement::FirstOrderLag(FirstOrderLag {
                input: OUT,
                output: PV,
                time_constant: 1.0,
                initial: 0.0,
            }));
        let sim = SimDriver::new(channel_map).unwrap();
        let point_map: dcs_runtime::PointMap = [
            (PV, Direction::In, dcs_core::ValueKind::Float),
            (SP, Direction::In, dcs_core::ValueKind::Float),
            (OUT, Direction::Out, dcs_core::ValueKind::Float),
        ]
        .into_iter()
        .collect();

        let pid = Pid::new(
            "pid",
            SP,
            PV,
            OUT,
            PidConfig {
                kp: 2.0,
                ki: 1.0,
                kd: 0.0,
                dt: 0.1,
                out_min: 0.0,
                out_max: 15.0,
            },
        )
        .unwrap();
        let mut executor = Executor::new(&sim, point_map, vec![Box::new(pid)]).unwrap();

        // Setpoint 10, pv starting at 0; run 40 seconds of simulated time.
        sim.write(SP, Value::Float(10.0)).unwrap();
        for _ in 0..400 {
            executor.scan().unwrap();
            sim.step(0.1);
        }

        let Value::Float(pv) = sim.read(PV).unwrap().value else {
            panic!("pv must be Float")
        };
        // Stated tolerance: within 0.5% of the 10-unit setpoint step.
        assert!((pv - 10.0).abs() < 0.05, "pv={pv}");
        assert_eq!(executor.component_statuses()[0].step_errors, 0);
    }
}
