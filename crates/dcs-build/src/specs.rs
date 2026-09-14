//! One [`Spec`] per registered `dcs-blocks` component kind.
//!
//! Each spec declares, as data, the vocabulary the kind's `describe()`
//! reports: the model `kind` string, the named ports with their
//! directions and value kinds (in `io_requirements` order), and the
//! parameter set `from_parameters` reads — name, kind, declared range,
//! and whether the parameter is required. `dcs-build` cannot depend on
//! `dcs-blocks`, so these are data mirrors kept honest by the
//! convention: a kind's spec lists exactly what its descriptor reports,
//! and `dcs-blocks/tests/spec_drift.rs` fails when the two drift apart
//! or when a registered kind has no spec.
//!
//! [`PlantBuilder::add`](crate::PlantBuilder::add) takes a spec and
//! returns the matching `*Instance` struct: typed [`Source`] /
//! [`Sink`] handles for each port, so `connect` checks direction and
//! value kind at compile time.

use crate::endpoint::{Sink, Source};
use crate::spec::{
    FINITE_F64, FRACTION_F64, NONNEGATIVE_F64, NONNEGATIVE_INT, POSITIVE_F64, ParamDecl,
    Parameters, PortDecl, Spec, optional, port, required,
};
use dcs_core::{Direction, PointType, ValueKind};
use dcs_model::ComponentId;
use std::marker::PhantomData;

mod sealed {
    /// Seals [`RawKind`](super::RawKind): the analog kinds' raw channel
    /// accepts exactly `i64` and `f64` — `Bool` raw is meaningless for
    /// scaling.
    pub trait Sealed {}

    impl Sealed for i64 {}
    impl Sealed for f64 {}
}

/// A raw-channel value kind: the `i64` and `f64` variants the
/// `analog-input`/`analog-output` kinds register. Sealed, mirroring the
/// kinds' own `RawInput`/`RawOutput` bounds, so no spec exists for a
/// variant the kind cannot build.
pub trait RawKind: PointType + sealed::Sealed {}

impl RawKind for i64 {}
impl RawKind for f64 {}

/// The scaling parameters `analog-input` and `analog-output` share: the
/// four required finite `Float` bounds `from_parameters` reads.
const SCALING_PARAMETERS: &[ParamDecl] = &[
    required("raw_min", ValueKind::Float, Some(FINITE_F64)),
    required("raw_max", ValueKind::Float, Some(FINITE_F64)),
    required("eng_min", ValueKind::Float, Some(FINITE_F64)),
    required("eng_max", ValueKind::Float, Some(FINITE_F64)),
];

/// Spec for the `analog-input` kind: raw-range to engineering-unit
/// scaling.
///
/// `R` is the raw channel's value type — `f64` or `i64` — matching the
/// kind's registered `f64`/`i64` variants. Ports mirror the descriptor:
/// `raw` (`In`, `R`), `out` (`Out`, `f64`); parameters are the four
/// required scaling bounds.
pub struct AnalogInputSpec<R: RawKind> {
    /// The instance's parameter map: the four scaling bounds, each a
    /// finite `Float`.
    pub parameters: Parameters,
    marker: PhantomData<fn() -> R>,
}

/// Typed port handles for an `analog-input` instance.
pub struct AnalogInputInstance<R: ?Sized> {
    /// The allocated component id.
    pub id: ComponentId,
    /// `raw` port (`In`, `R`): the field measurement to scale.
    pub raw: Sink<R>,
    /// `out` port (`Out`, `f64`): the scaled engineering value.
    pub out: Source<f64>,
}

impl<R: RawKind> AnalogInputSpec<R> {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "analog-input";

    /// The declared parameter set: `raw_min`, `raw_max`, `eng_min`,
    /// `eng_max` — all required finite `Float`s.
    pub const PARAMETERS: &'static [ParamDecl] = SCALING_PARAMETERS;

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self {
            parameters,
            marker: PhantomData,
        }
    }
}

impl<R: RawKind> Spec for AnalogInputSpec<R> {
    type Instance = AnalogInputInstance<R>;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("raw", Direction::In, R::KIND),
            port("out", Direction::Out, ValueKind::Float),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        AnalogInputInstance {
            id,
            raw: Sink::port(id, "raw"),
            out: Source::port(id, "out"),
        }
    }
}

/// Spec for the `analog-output` kind: engineering-unit to raw-range
/// scaling, the inverse of [`AnalogInputSpec`].
///
/// `R` is the raw channel's value type — `f64` or `i64`. Ports mirror
/// the descriptor: `eng` (`In`, `f64`), `raw` (`Out`, `R`); parameters
/// are the four required scaling bounds.
pub struct AnalogOutputSpec<R: RawKind> {
    /// The instance's parameter map: the four scaling bounds, each a
    /// finite `Float`.
    pub parameters: Parameters,
    marker: PhantomData<fn() -> R>,
}

/// Typed port handles for an `analog-output` instance.
pub struct AnalogOutputInstance<R: ?Sized> {
    /// The allocated component id.
    pub id: ComponentId,
    /// `eng` port (`In`, `f64`): the engineering value to drive.
    pub eng: Sink<f64>,
    /// `raw` port (`Out`, `R`): the raw field output.
    pub raw: Source<R>,
}

impl<R: RawKind> AnalogOutputSpec<R> {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "analog-output";

    /// The declared parameter set: `raw_min`, `raw_max`, `eng_min`,
    /// `eng_max` — all required finite `Float`s.
    pub const PARAMETERS: &'static [ParamDecl] = SCALING_PARAMETERS;

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self {
            parameters,
            marker: PhantomData,
        }
    }
}

impl<R: RawKind> Spec for AnalogOutputSpec<R> {
    type Instance = AnalogOutputInstance<R>;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("eng", Direction::In, ValueKind::Float),
            port("raw", Direction::Out, R::KIND),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        AnalogOutputInstance {
            id,
            eng: Sink::port(id, "eng"),
            raw: Source::port(id, "raw"),
        }
    }
}

/// Spec for the `pid` kind: parallel-form PID control with output
/// limits.
///
/// Ports mirror the descriptor: `sp` (`In`, `Float`), `pv` (`In`,
/// `Float`), `out` (`Out`, `Float`). Parameters: `kp`, `dt`, `out_min`,
/// `out_max` required (`dt` strictly positive); `ki`, `kd` optional —
/// all `Float`s.
pub struct PidSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
}

/// Typed port handles for a `pid` instance.
pub struct PidInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `sp` port (`In`, `Float`): the setpoint to regulate toward.
    pub sp: Sink<f64>,
    /// `pv` port (`In`, `Float`): the measured process value.
    pub pv: Sink<f64>,
    /// `out` port (`Out`, `Float`): the manipulated variable.
    pub out: Source<f64>,
}

impl PidSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "pid";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] = &[
        required("kp", ValueKind::Float, Some(FINITE_F64)),
        optional("ki", ValueKind::Float, Some(FINITE_F64)),
        optional("kd", ValueKind::Float, Some(FINITE_F64)),
        required("dt", ValueKind::Float, Some(POSITIVE_F64)),
        required("out_min", ValueKind::Float, Some(FINITE_F64)),
        required("out_max", ValueKind::Float, Some(FINITE_F64)),
    ];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for PidSpec {
    type Instance = PidInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("sp", Direction::In, ValueKind::Float),
            port("pv", Direction::In, ValueKind::Float),
            port("out", Direction::Out, ValueKind::Float),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        PidInstance {
            id,
            sp: Sink::port(id, "sp"),
            pv: Sink::port(id, "pv"),
            out: Source::port(id, "out"),
        }
    }
}

/// Spec for the `digital-input` kind: boolean read with optional
/// inversion and tick-based debounce.
///
/// Ports mirror the descriptor: `in` (`In`, `Bool`), `out` (`Out`,
/// `Bool`). Parameters: `invert` (optional `Bool`), `debounce_ticks`
/// (optional non-negative `Int`).
pub struct DigitalInputSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
}

/// Typed port handles for a `digital-input` instance.
pub struct DigitalInputInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `in` port (`In`, `Bool`): the field signal to condition.
    pub input: Sink<bool>,
    /// `out` port (`Out`, `Bool`): the conditioned value.
    pub out: Source<bool>,
}

impl DigitalInputSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "digital-input";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] = &[
        optional("invert", ValueKind::Bool, None),
        optional("debounce_ticks", ValueKind::Int, Some(NONNEGATIVE_INT)),
    ];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for DigitalInputSpec {
    type Instance = DigitalInputInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("in", Direction::In, ValueKind::Bool),
            port("out", Direction::Out, ValueKind::Bool),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        DigitalInputInstance {
            id,
            input: Sink::port(id, "in"),
            out: Source::port(id, "out"),
        }
    }
}

/// Spec for the `digital-output` kind: boolean write with quality
/// propagation.
///
/// Ports mirror the descriptor: `in` (`In`, `Bool`), `out` (`Out`,
/// `Bool`). Parameter: `invert` (optional `Bool`).
pub struct DigitalOutputSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
}

/// Typed port handles for a `digital-output` instance.
pub struct DigitalOutputInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `in` port (`In`, `Bool`): the value to drive.
    pub input: Sink<bool>,
    /// `out` port (`Out`, `Bool`): the field output.
    pub out: Source<bool>,
}

impl DigitalOutputSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "digital-output";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] = &[optional("invert", ValueKind::Bool, None)];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for DigitalOutputSpec {
    type Instance = DigitalOutputInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("in", Direction::In, ValueKind::Bool),
            port("out", Direction::Out, ValueKind::Bool),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        DigitalOutputInstance {
            id,
            input: Sink::port(id, "in"),
            out: Source::port(id, "out"),
        }
    }
}

/// Spec for the `alarm-monitor` kind: high/low limit checking with
/// hysteresis on an analog signal.
///
/// Ports mirror the descriptor: `in` (`In`, `Float`), `alarm` (`Out`,
/// `Bool`). Parameters: `low_limit`, `high_limit` required finite
/// `Float`s; `hysteresis` optional non-negative `Float`.
pub struct AlarmMonitorSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
}

/// Typed port handles for an `alarm-monitor` instance.
pub struct AlarmMonitorInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `in` port (`In`, `Float`): the monitored analog value.
    pub input: Sink<f64>,
    /// `alarm` port (`Out`, `Bool`): the limit-violation state.
    pub alarm: Source<bool>,
}

impl AlarmMonitorSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "alarm-monitor";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] = &[
        required("low_limit", ValueKind::Float, Some(FINITE_F64)),
        required("high_limit", ValueKind::Float, Some(FINITE_F64)),
        optional("hysteresis", ValueKind::Float, Some(NONNEGATIVE_F64)),
    ];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for AlarmMonitorSpec {
    type Instance = AlarmMonitorInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("in", Direction::In, ValueKind::Float),
            port("alarm", Direction::Out, ValueKind::Bool),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        AlarmMonitorInstance {
            id,
            input: Sink::port(id, "in"),
            alarm: Source::port(id, "alarm"),
        }
    }
}

/// Spec for the `interlock` kind: analog pass-through gated by Bool
/// trip inputs and a permissive.
///
/// Ports mirror the descriptor: `in` (`In`, `Float`), `permissive`
/// (`In`, `Bool`), the `trip_1`…`trip_N` set (`In`, `Bool`, one per
/// [`trips`](Self::trips)), `out` (`Out`, `Float`), `tripped` (`Out`,
/// `Bool`). Parameter: `safe_value` (required finite `Float`).
pub struct InterlockSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
    /// How many `trip_N` inputs the instance declares.
    pub trips: usize,
}

/// Typed port handles for an `interlock` instance.
pub struct InterlockInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `in` port (`In`, `Float`): the value passed through while clear.
    pub input: Sink<f64>,
    /// `permissive` port (`In`, `Bool`): the enable condition.
    pub permissive: Sink<bool>,
    /// `out` port (`Out`, `Float`): the passed-through or safe value.
    pub out: Source<f64>,
    /// `tripped` port (`Out`, `Bool`): whether any trip input is set.
    pub tripped: Source<bool>,
}

impl InterlockInstance {
    /// The `index`th trip input (`trip_1`…`trip_N`): an `In`, `Bool`
    /// port. The trip count lives on the spec, not this handle — an
    /// index outside `1..=N` names a port the instance does not declare,
    /// and [`build`](crate::PlantBuilder::build) reports it.
    pub fn trip(&self, index: usize) -> Sink<bool> {
        Sink::port(self.id, &format!("trip_{index}"))
    }
}

impl InterlockSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "interlock";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] =
        &[required("safe_value", ValueKind::Float, Some(FINITE_F64))];

    /// A spec for an instance declaring `trips` trip inputs.
    pub fn new(parameters: Parameters, trips: usize) -> Self {
        Self { parameters, trips }
    }
}

impl Spec for InterlockSpec {
    type Instance = InterlockInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        let mut ports = vec![
            port("in", Direction::In, ValueKind::Float),
            port("permissive", Direction::In, ValueKind::Bool),
        ];
        ports.extend(
            (1..=self.trips)
                .map(|index| port(&format!("trip_{index}"), Direction::In, ValueKind::Bool)),
        );
        ports.push(port("out", Direction::Out, ValueKind::Float));
        ports.push(port("tripped", Direction::Out, ValueKind::Bool));
        ports
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        InterlockInstance {
            id,
            input: Sink::port(id, "in"),
            permissive: Sink::port(id, "permissive"),
            out: Source::port(id, "out"),
            tripped: Source::port(id, "tripped"),
        }
    }
}

/// Spec for the `override-select` kind: deterministic selection between
/// a control and an operator value.
///
/// Ports mirror the descriptor: `control` (`In`, `Float`), `operator`
/// (`In`, `Float`), `select` (`In`, `Bool`), `out` (`Out`, `Float`).
/// The kind takes no parameters.
pub struct OverrideSelectSpec {
    /// The instance's parameter map — the kind declares no parameters,
    /// so any key is an [`UnknownParameter`](crate::BuildError::UnknownParameter)
    /// at `build`.
    pub parameters: Parameters,
}

/// Typed port handles for an `override-select` instance.
pub struct OverrideSelectInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `control` port (`In`, `Float`): the loop's manipulated value.
    pub control: Sink<f64>,
    /// `operator` port (`In`, `Float`): the operator's override value.
    pub operator: Sink<f64>,
    /// `select` port (`In`, `Bool`): which input drives `out`.
    pub select: Sink<bool>,
    /// `out` port (`Out`, `Float`): the selected value.
    pub out: Source<f64>,
}

impl OverrideSelectSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "override-select";

    /// The declared parameter set: the kind takes none.
    pub const PARAMETERS: &'static [ParamDecl] = &[];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for OverrideSelectSpec {
    type Instance = OverrideSelectInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("control", Direction::In, ValueKind::Float),
            port("operator", Direction::In, ValueKind::Float),
            port("select", Direction::In, ValueKind::Bool),
            port("out", Direction::Out, ValueKind::Float),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        OverrideSelectInstance {
            id,
            control: Sink::port(id, "control"),
            operator: Sink::port(id, "operator"),
            select: Sink::port(id, "select"),
            out: Source::port(id, "out"),
        }
    }
}

/// Spec for the `valve` kind: analog actuator with a position-feedback
/// discrepancy diagnostic.
///
/// Ports mirror the descriptor: `cmd` (`In`, `Float`), `out` (`Out`,
/// `Float`), `fb` (`In`, `Float`), `discrepancy` (`Out`, `Bool`).
/// Parameters: `tolerance` (required non-negative `Float`),
/// `discrepancy_ticks` (optional non-negative `Int`).
pub struct ValveSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
}

/// Typed port handles for a `valve` instance.
pub struct ValveInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `cmd` port (`In`, `Float`): the commanded position.
    pub cmd: Sink<f64>,
    /// `out` port (`Out`, `Float`): the driven output.
    pub out: Source<f64>,
    /// `fb` port (`In`, `Float`): the position feedback.
    pub fb: Sink<f64>,
    /// `discrepancy` port (`Out`, `Bool`): command-versus-feedback fault.
    pub discrepancy: Source<bool>,
}

impl ValveSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "valve";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] = &[
        required("tolerance", ValueKind::Float, Some(NONNEGATIVE_F64)),
        optional("discrepancy_ticks", ValueKind::Int, Some(NONNEGATIVE_INT)),
    ];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for ValveSpec {
    type Instance = ValveInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("cmd", Direction::In, ValueKind::Float),
            port("out", Direction::Out, ValueKind::Float),
            port("fb", Direction::In, ValueKind::Float),
            port("discrepancy", Direction::Out, ValueKind::Bool),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        ValveInstance {
            id,
            cmd: Sink::port(id, "cmd"),
            out: Source::port(id, "out"),
            fb: Sink::port(id, "fb"),
            discrepancy: Source::port(id, "discrepancy"),
        }
    }
}

/// Spec for the `motor` kind: discrete actuator with a run-feedback
/// fault diagnostic.
///
/// Ports mirror the descriptor: `cmd` (`In`, `Bool`), `out` (`Out`,
/// `Bool`), `run` (`In`, `Bool`), `fault` (`Out`, `Bool`). Parameter:
/// `fault_ticks` (optional non-negative `Int`).
pub struct MotorSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
}

/// Typed port handles for a `motor` instance.
pub struct MotorInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `cmd` port (`In`, `Bool`): the run command.
    pub cmd: Sink<bool>,
    /// `out` port (`Out`, `Bool`): the driven contactor output.
    pub out: Source<bool>,
    /// `run` port (`In`, `Bool`): the run feedback.
    pub run: Sink<bool>,
    /// `fault` port (`Out`, `Bool`): command-versus-feedback fault.
    pub fault: Source<bool>,
}

impl MotorSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "motor";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] = &[optional(
        "fault_ticks",
        ValueKind::Int,
        Some(NONNEGATIVE_INT),
    )];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for MotorSpec {
    type Instance = MotorInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("cmd", Direction::In, ValueKind::Bool),
            port("out", Direction::Out, ValueKind::Bool),
            port("run", Direction::In, ValueKind::Bool),
            port("fault", Direction::Out, ValueKind::Bool),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        MotorInstance {
            id,
            cmd: Sink::port(id, "cmd"),
            out: Source::port(id, "out"),
            run: Sink::port(id, "run"),
            fault: Source::port(id, "fault"),
        }
    }
}

/// Spec for the `timer` kind: on-delay/off-delay timing of a Boolean
/// signal in ticks.
///
/// Ports mirror the descriptor: `in` (`In`, `Bool`), `out` (`Out`,
/// `Bool`). Parameters: `delay_ticks` (required non-negative `Int`),
/// `off_delay` (optional `Bool`).
pub struct TimerSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
}

/// Typed port handles for a `timer` instance.
pub struct TimerInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `in` port (`In`, `Bool`): the signal to time.
    pub input: Sink<bool>,
    /// `out` port (`Out`, `Bool`): the timed value.
    pub out: Source<bool>,
}

impl TimerSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "timer";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] = &[
        required("delay_ticks", ValueKind::Int, Some(NONNEGATIVE_INT)),
        optional("off_delay", ValueKind::Bool, None),
    ];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for TimerSpec {
    type Instance = TimerInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("in", Direction::In, ValueKind::Bool),
            port("out", Direction::Out, ValueKind::Bool),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        TimerInstance {
            id,
            input: Sink::port(id, "in"),
            out: Source::port(id, "out"),
        }
    }
}

/// Spec for the `counter` kind: rising-edge counting with a
/// preset-reached flag and a reset input.
///
/// Ports mirror the descriptor: `in` (`In`, `Bool`), `reset` (`In`,
/// `Bool`), `count` (`Out`, `Int`), `done` (`Out`, `Bool`). Parameter:
/// `preset` (required non-negative `Int`).
pub struct CounterSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
}

/// Typed port handles for a `counter` instance.
pub struct CounterInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `in` port (`In`, `Bool`): the signal whose rising edges count.
    pub input: Sink<bool>,
    /// `reset` port (`In`, `Bool`): clears the count.
    pub reset: Sink<bool>,
    /// `count` port (`Out`, `Int`): the accumulated count.
    pub count: Source<i64>,
    /// `done` port (`Out`, `Bool`): the preset-reached flag.
    pub done: Source<bool>,
}

impl CounterSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "counter";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] =
        &[required("preset", ValueKind::Int, Some(NONNEGATIVE_INT))];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for CounterSpec {
    type Instance = CounterInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("in", Direction::In, ValueKind::Bool),
            port("reset", Direction::In, ValueKind::Bool),
            port("count", Direction::Out, ValueKind::Int),
            port("done", Direction::Out, ValueKind::Bool),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        CounterInstance {
            id,
            input: Sink::port(id, "in"),
            reset: Sink::port(id, "reset"),
            count: Source::port(id, "count"),
            done: Source::port(id, "done"),
        }
    }
}

/// Spec for the `rate-limiter` kind: an analog output slewing toward its
/// input by a bounded per-tick delta.
///
/// Ports mirror the descriptor: `in` (`In`, `Float`), `out` (`Out`,
/// `Float`). Parameter: `max_delta` (required positive `Float`).
pub struct RateLimiterSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
}

/// Typed port handles for a `rate-limiter` instance.
pub struct RateLimiterInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `in` port (`In`, `Float`): the target value.
    pub input: Sink<f64>,
    /// `out` port (`Out`, `Float`): the slew-limited value.
    pub out: Source<f64>,
}

impl RateLimiterSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "rate-limiter";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] =
        &[required("max_delta", ValueKind::Float, Some(POSITIVE_F64))];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for RateLimiterSpec {
    type Instance = RateLimiterInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("in", Direction::In, ValueKind::Float),
            port("out", Direction::Out, ValueKind::Float),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        RateLimiterInstance {
            id,
            input: Sink::port(id, "in"),
            out: Source::port(id, "out"),
        }
    }
}

/// Spec for the `latching-alarm` kind: high/low limit checking with
/// hysteresis plus an operator-acknowledgment latch.
///
/// Ports mirror the descriptor: `in` (`In`, `Float`), `ack` (`In`,
/// `Bool`), `alarm` (`Out`, `Bool`), `unacknowledged` (`Out`, `Bool`).
/// Parameters — shared with `alarm-monitor`: `low_limit`, `high_limit`
/// required finite `Float`s; `hysteresis` optional non-negative
/// `Float`.
pub struct LatchingAlarmSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
}

/// Typed port handles for a `latching-alarm` instance.
pub struct LatchingAlarmInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `in` port (`In`, `Float`): the monitored analog value.
    pub input: Sink<f64>,
    /// `ack` port (`In`, `Bool`): the operator's clearing command.
    pub ack: Sink<bool>,
    /// `alarm` port (`Out`, `Bool`): the limit-violation state.
    pub alarm: Source<bool>,
    /// `unacknowledged` port (`Out`, `Bool`): the
    /// trip-until-acknowledged latch.
    pub unacknowledged: Source<bool>,
}

impl LatchingAlarmSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "latching-alarm";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] = &[
        required("low_limit", ValueKind::Float, Some(FINITE_F64)),
        required("high_limit", ValueKind::Float, Some(FINITE_F64)),
        optional("hysteresis", ValueKind::Float, Some(NONNEGATIVE_F64)),
    ];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for LatchingAlarmSpec {
    type Instance = LatchingAlarmInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("in", Direction::In, ValueKind::Float),
            port("ack", Direction::In, ValueKind::Bool),
            port("alarm", Direction::Out, ValueKind::Bool),
            port("unacknowledged", Direction::Out, ValueKind::Bool),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        LatchingAlarmInstance {
            id,
            input: Sink::port(id, "in"),
            ack: Sink::port(id, "ack"),
            alarm: Source::port(id, "alarm"),
            unacknowledged: Source::port(id, "unacknowledged"),
        }
    }
}

/// Spec for the `manual-station` kind: operator-selectable source on an
/// analog output with a slew-bounded bumpless transfer.
///
/// Ports mirror the descriptor: `control` (`In`, `Float`), `manual`
/// (`In`, `Float`), `mode` (`In`, `Bool`), `out` (`Out`, `Float`),
/// `manual_active` (`Out`, `Bool`). Parameter: `transfer_delta`
/// (required positive `Float`).
pub struct ManualStationSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
}

/// Typed port handles for a `manual-station` instance.
pub struct ManualStationInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `control` port (`In`, `Float`): the process-side value.
    pub control: Sink<f64>,
    /// `manual` port (`In`, `Float`): the operator's entered value.
    pub manual: Sink<f64>,
    /// `mode` port (`In`, `Bool`): which source `out` follows.
    pub mode: Sink<bool>,
    /// `out` port (`Out`, `Float`): the driven value.
    pub out: Source<f64>,
    /// `manual_active` port (`Out`, `Bool`): the reported selection.
    pub manual_active: Source<bool>,
}

impl ManualStationSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "manual-station";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] = &[required(
        "transfer_delta",
        ValueKind::Float,
        Some(POSITIVE_F64),
    )];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for ManualStationSpec {
    type Instance = ManualStationInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("control", Direction::In, ValueKind::Float),
            port("manual", Direction::In, ValueKind::Float),
            port("mode", Direction::In, ValueKind::Bool),
            port("out", Direction::Out, ValueKind::Float),
            port("manual_active", Direction::Out, ValueKind::Bool),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        ManualStationInstance {
            id,
            control: Sink::port(id, "control"),
            manual: Sink::port(id, "manual"),
            mode: Sink::port(id, "mode"),
            out: Source::port(id, "out"),
            manual_active: Source::port(id, "manual_active"),
        }
    }
}

/// Spec for the `signal-filter` kind: a first-order per-tick smoothing
/// of an analog signal.
///
/// Ports mirror the descriptor: `in` (`In`, `Float`), `out` (`Out`,
/// `Float`). Parameter: `alpha` (required `Float` in `(0, 1]`).
pub struct SignalFilterSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
}

/// Typed port handles for a `signal-filter` instance.
pub struct SignalFilterInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `in` port (`In`, `Float`): the value to smooth.
    pub input: Sink<f64>,
    /// `out` port (`Out`, `Float`): the filtered estimate.
    pub out: Source<f64>,
}

impl SignalFilterSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "signal-filter";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] =
        &[required("alpha", ValueKind::Float, Some(FRACTION_F64))];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for SignalFilterSpec {
    type Instance = SignalFilterInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("in", Direction::In, ValueKind::Float),
            port("out", Direction::Out, ValueKind::Float),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        SignalFilterInstance {
            id,
            input: Sink::port(id, "in"),
            out: Source::port(id, "out"),
        }
    }
}

/// Spec for the `median-voter` kind: 2oo3 median voting over three
/// redundant analog inputs with a spread discrepancy diagnostic.
///
/// Ports mirror the descriptor: `in_1`, `in_2`, `in_3` (`In`, `Float`),
/// `out` (`Out`, `Float`), `discrepancy` (`Out`, `Bool`). Parameter:
/// `tolerance` (required non-negative `Float`).
pub struct MedianVoterSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
}

/// Typed port handles for a `median-voter` instance.
pub struct MedianVoterInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `in_1` port (`In`, `Float`): the first redundant measurement.
    pub in_1: Sink<f64>,
    /// `in_2` port (`In`, `Float`): the second redundant measurement.
    pub in_2: Sink<f64>,
    /// `in_3` port (`In`, `Float`): the third redundant measurement.
    pub in_3: Sink<f64>,
    /// `out` port (`Out`, `Float`): the voted value.
    pub out: Source<f64>,
    /// `discrepancy` port (`Out`, `Bool`): the spread-exceeded flag.
    pub discrepancy: Source<bool>,
}

impl MedianVoterSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "median-voter";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] = &[required(
        "tolerance",
        ValueKind::Float,
        Some(NONNEGATIVE_F64),
    )];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for MedianVoterSpec {
    type Instance = MedianVoterInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("in_1", Direction::In, ValueKind::Float),
            port("in_2", Direction::In, ValueKind::Float),
            port("in_3", Direction::In, ValueKind::Float),
            port("out", Direction::Out, ValueKind::Float),
            port("discrepancy", Direction::Out, ValueKind::Bool),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        MedianVoterInstance {
            id,
            in_1: Sink::port(id, "in_1"),
            in_2: Sink::port(id, "in_2"),
            in_3: Sink::port(id, "in_3"),
            out: Source::port(id, "out"),
            discrepancy: Source::port(id, "discrepancy"),
        }
    }
}

/// Spec for the `totalizer` kind: a rate input accumulated into a
/// running total, with a reset input and a configurable rollover.
///
/// Ports mirror the descriptor: `rate` (`In`, `Float`), `reset` (`In`,
/// `Bool`), `total` (`Out`, `Float`). Parameters: `rate_unit`
/// (optional positive `Float`), `rollover` (optional non-negative
/// `Float`).
pub struct TotalizerSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
}

/// Typed port handles for a `totalizer` instance.
pub struct TotalizerInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `rate` port (`In`, `Float`): the rate being accumulated.
    pub rate: Sink<f64>,
    /// `reset` port (`In`, `Bool`): clears the total.
    pub reset: Sink<bool>,
    /// `total` port (`Out`, `Float`): the running total.
    pub total: Source<f64>,
}

impl TotalizerSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "totalizer";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] = &[
        optional("rate_unit", ValueKind::Float, Some(POSITIVE_F64)),
        optional("rollover", ValueKind::Float, Some(NONNEGATIVE_F64)),
    ];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for TotalizerSpec {
    type Instance = TotalizerInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("rate", Direction::In, ValueKind::Float),
            port("reset", Direction::In, ValueKind::Bool),
            port("total", Direction::Out, ValueKind::Float),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        TotalizerInstance {
            id,
            rate: Sink::port(id, "rate"),
            reset: Sink::port(id, "reset"),
            total: Source::port(id, "total"),
        }
    }
}

/// Spec for the `sequencer` kind: stepping through a declared ordered
/// table of steps, each driving `out` for a configured tick count.
///
/// Ports mirror the descriptor: `run` (`In`, `Bool`), `reset` (`In`,
/// `Bool`), `out` (`Out`, `Float`), `step` (`Out`, `Int`), `done`
/// (`Out`, `Bool`).
///
/// The parameter set is *not statically enumerable*: `step_count`
/// declares the table length `N` and each step `n` in `1..=N` adds
/// `step_<n>_ticks` (required non-negative `Int`) and `step_<n>_out`
/// (required finite `Float`) — a different key set per instance. The
/// chosen treatment is [`declared_parameters`](Spec::declared_parameters)
/// `None`: the instance's parameter map goes unchecked at
/// [`build`](crate::PlantBuilder::build), and the kind's
/// `from_parameters` remains the authority, reporting a named
/// `ParameterError` for a missing or mistyped `step_count` or
/// `step_<n>_*` entry when the emitted document assembles.
pub struct SequencerSpec {
    /// The instance's parameter map: `step_count` plus the per-step
    /// `step_<n>_ticks`/`step_<n>_out` entries.
    pub parameters: Parameters,
}

/// Typed port handles for a `sequencer` instance.
pub struct SequencerInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `run` port (`In`, `Bool`): the signal stepping the table.
    pub run: Sink<bool>,
    /// `reset` port (`In`, `Bool`): the return-to-start condition.
    pub reset: Sink<bool>,
    /// `out` port (`Out`, `Float`): the active step's driven value.
    pub out: Source<f64>,
    /// `step` port (`Out`, `Int`): the active step's 1-based index.
    pub step: Source<i64>,
    /// `done` port (`Out`, `Bool`): the table-run-to-end flag.
    pub done: Source<bool>,
}

impl SequencerSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "sequencer";

    /// A spec carrying `parameters` as the instance's parameter map.
    ///
    /// The map must carry `step_count` (`Int`, at least 1) plus
    /// `step_<n>_ticks` (`Int`) and `step_<n>_out` (`Float`) for every
    /// `n` in `1..=step_count` — checked by the kind's
    /// `from_parameters` at assembly, not by
    /// [`build`](crate::PlantBuilder::build).
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for SequencerSpec {
    type Instance = SequencerInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("run", Direction::In, ValueKind::Bool),
            port("reset", Direction::In, ValueKind::Bool),
            port("out", Direction::Out, ValueKind::Float),
            port("step", Direction::Out, ValueKind::Int),
            port("done", Direction::Out, ValueKind::Bool),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        // The step-table keys are indexed by the instance's
        // `step_count`: no static set exists, so the map goes
        // unchecked at `build` (the recorded treatment for this kind).
        None
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        SequencerInstance {
            id,
            run: Sink::port(id, "run"),
            reset: Sink::port(id, "reset"),
            out: Source::port(id, "out"),
            step: Source::port(id, "step"),
            done: Source::port(id, "done"),
        }
    }
}
