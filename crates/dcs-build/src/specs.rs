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
    BINARY_CODE_RANGE, CODE_RANGE, FINITE_F64, FRACTION_F64, NONNEGATIVE_F64, NONNEGATIVE_INT,
    POSITIVE_F64, POSITIVE_INT, ParamDecl, Parameters, PortDecl, Spec, optional, port, required,
};
use dcs_core::{Direction, PointType, ValueKind};
use dcs_model::{ComponentId, Rationalization};
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
/// `Float` — plus the decision-70 rationalization codes `priority`,
/// `class`, `response_ticks`, required non-negative `Int`s.
/// `rationalization` is the record's required prose half — a typed
/// argument so a composed plant cannot omit it.
pub struct LatchingAlarmSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
    /// The instance's decision-70 rationalization record.
    pub rationalization: Rationalization,
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
        required("priority", ValueKind::Int, Some(NONNEGATIVE_INT)),
        required("class", ValueKind::Int, Some(NONNEGATIVE_INT)),
        required("response_ticks", ValueKind::Int, Some(NONNEGATIVE_INT)),
    ];

    /// A spec carrying `parameters` as the instance's parameter map and
    /// `rationalization` as the instance's required decision-70 record.
    pub fn new(parameters: Parameters, rationalization: Rationalization) -> Self {
        Self {
            parameters,
            rationalization,
        }
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

    fn rationalization(&self) -> Option<&Rationalization> {
        Some(&self.rationalization)
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

/// Spec for the `bool-latching-alarm` kind: the two-flag alarm
/// lifecycle for Bool-sourced conditions — `latching-alarm`'s Bool
/// sibling, where `alarm` follows `in` and `unacknowledged` latches a
/// fresh assertion until `ack` reads `true`.
///
/// Ports mirror the descriptor: `in` (`In`, `Bool`), `ack` (`In`,
/// `Bool`), `alarm` (`Out`, `Bool`), `unacknowledged` (`Out`, `Bool`).
/// Parameters are the decision-70 rationalization codes `priority`,
/// `class`, `response_ticks` — required non-negative `Int`s;
/// `rationalization` is the record's required prose half — a typed
/// argument so a composed plant cannot omit it.
pub struct BoolLatchingAlarmSpec {
    /// The instance's parameter map — the three required rationalization
    /// codes; any other key is an
    /// [`UnknownParameter`](crate::BuildError::UnknownParameter) at
    /// `build`.
    pub parameters: Parameters,
    /// The instance's decision-70 rationalization record.
    pub rationalization: Rationalization,
}

/// Typed port handles for a `bool-latching-alarm` instance.
pub struct BoolLatchingAlarmInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `in` port (`In`, `Bool`): the Bool alarm condition.
    pub input: Sink<bool>,
    /// `ack` port (`In`, `Bool`): the operator's clearing command.
    pub ack: Sink<bool>,
    /// `alarm` port (`Out`, `Bool`): the standing condition state.
    pub alarm: Source<bool>,
    /// `unacknowledged` port (`Out`, `Bool`): the
    /// asserted-until-acknowledged latch.
    pub unacknowledged: Source<bool>,
}

impl BoolLatchingAlarmSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "bool-latching-alarm";

    /// The declared parameter set — the decision-70 rationalization
    /// codes.
    pub const PARAMETERS: &'static [ParamDecl] = &[
        required("priority", ValueKind::Int, Some(NONNEGATIVE_INT)),
        required("class", ValueKind::Int, Some(NONNEGATIVE_INT)),
        required("response_ticks", ValueKind::Int, Some(NONNEGATIVE_INT)),
    ];

    /// A spec carrying `parameters` as the instance's parameter map and
    /// `rationalization` as the instance's required decision-70 record.
    pub fn new(parameters: Parameters, rationalization: Rationalization) -> Self {
        Self {
            parameters,
            rationalization,
        }
    }
}

impl Spec for BoolLatchingAlarmSpec {
    type Instance = BoolLatchingAlarmInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("in", Direction::In, ValueKind::Bool),
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

    fn rationalization(&self) -> Option<&Rationalization> {
        Some(&self.rationalization)
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        BoolLatchingAlarmInstance {
            id,
            input: Sink::port(id, "in"),
            ack: Sink::port(id, "ack"),
            alarm: Source::port(id, "alarm"),
            unacknowledged: Source::port(id, "unacknowledged"),
        }
    }
}

/// The managed-alarm surface decisions 71–73 record, shared by both
/// managed latching kinds' specs: which of the optional `shelve`,
/// `oos`, and `suppress` inputs the instance declares.
///
/// A `false` flag emits an instance with no port to wire — the model's
/// "unbound" case: no shelving surface, never out of service, never
/// suppressed. The status outputs `shelved`, `suppressed`, and
/// `out_of_service` are always declared — the uniform Status-role
/// vocabulary the alarm pane joins.
#[derive(Debug, Clone, Copy, Default)]
pub struct ManagedInputs {
    /// Whether the instance declares the `shelve` port — the
    /// level-observed shelving request, conventionally wired to a
    /// writable internal `In` point.
    pub shelve: bool,
    /// Whether the instance declares the `oos` port — the
    /// level-observed out-of-service command.
    pub oos: bool,
    /// Whether the instance declares the `suppress` port — the
    /// designed-suppression condition.
    pub suppress: bool,
}

/// The bound managed inputs' port declarations, in
/// `shelve`/`oos`/`suppress` order — the position the kinds'
/// `io_requirements` place them.
fn managed_input_ports(inputs: ManagedInputs) -> Vec<PortDecl> {
    let mut ports = Vec::with_capacity(3);
    if inputs.shelve {
        ports.push(port("shelve", Direction::In, ValueKind::Bool));
    }
    if inputs.oos {
        ports.push(port("oos", Direction::In, ValueKind::Bool));
    }
    if inputs.suppress {
        ports.push(port("suppress", Direction::In, ValueKind::Bool));
    }
    ports
}

/// The three managed status outputs' declarations — always all three.
fn managed_output_ports() -> Vec<PortDecl> {
    vec![
        port("shelved", Direction::Out, ValueKind::Bool),
        port("suppressed", Direction::Out, ValueKind::Bool),
        port("out_of_service", Direction::Out, ValueKind::Bool),
    ]
}

/// The managed-alarm parameter set both managed latching kinds declare:
/// `max_shelve_ticks` — the shelving bound, `0` declaring
/// never-shelvable — plus the decision-70 `priority`/`class`/
/// `response_ticks` rationalization fields; all required non-negative
/// `Int`s.
const MANAGED_ALARM_PARAMETERS: &[ParamDecl] = &[
    required("max_shelve_ticks", ValueKind::Int, Some(NONNEGATIVE_INT)),
    required("priority", ValueKind::Int, Some(NONNEGATIVE_INT)),
    required("class", ValueKind::Int, Some(NONNEGATIVE_INT)),
    required("response_ticks", ValueKind::Int, Some(NONNEGATIVE_INT)),
];

/// Typed handles for the managed-alarm ports both managed latching
/// kinds share — the `Option` members following the declared set.
#[derive(Debug)]
pub struct ManagedAlarmHandles {
    /// `shelve` port (`In`, `Bool`): the level-observed shelving
    /// request — `Some` only when the spec declared the port; wiring a
    /// handle the emitted instance does not carry is `UnknownPort` at
    /// `build`. Wire it to a writable internal `In` point so operator
    /// shelve requests ride the receipted command path.
    pub shelve: Option<Sink<bool>>,
    /// `oos` port (`In`, `Bool`): the level-observed out-of-service
    /// command — `Some` only when declared.
    pub oos: Option<Sink<bool>>,
    /// `suppress` port (`In`, `Bool`): the designed-suppression
    /// condition — `Some` only when declared.
    pub suppress: Option<Sink<bool>>,
    /// `shelved` port (`Out`, `Bool`): asserts while a shelve request
    /// stands inside the declared bound.
    pub shelved: Source<bool>,
    /// `suppressed` port (`Out`, `Bool`): asserts while `suppress`
    /// reads `true`.
    pub suppressed: Source<bool>,
    /// `out_of_service` port (`Out`, `Bool`): asserts while `oos`
    /// reads `true`.
    pub out_of_service: Source<bool>,
}

impl ManagedAlarmHandles {
    /// Builds the handles for component `id` under `inputs` — the
    /// `Option` members `Some` only where the spec declared the port.
    fn for_component(id: ComponentId, inputs: ManagedInputs) -> Self {
        Self {
            shelve: inputs.shelve.then(|| Sink::port(id, "shelve")),
            oos: inputs.oos.then(|| Sink::port(id, "oos")),
            suppress: inputs.suppress.then(|| Sink::port(id, "suppress")),
            shelved: Source::port(id, "shelved"),
            suppressed: Source::port(id, "suppressed"),
            out_of_service: Source::port(id, "out_of_service"),
        }
    }
}

/// Spec for the `managed-latching-alarm` kind: `latching-alarm`'s
/// limit checking and acknowledgment latch plus the shelving,
/// suppression, and out-of-service lifecycle decisions 71–73 record.
///
/// Ports mirror the descriptor: `in` (`In`, `Float`), `ack` (`In`,
/// `Bool`), the spec-declared subset of `shelve`/`oos`/`suppress`
/// (`In`, `Bool`), then `alarm`, `unacknowledged`, `shelved`,
/// `suppressed`, `out_of_service` (all `Out`, `Bool`). Parameters — the
/// sibling's `low_limit`, `high_limit` (required finite `Float`s) and
/// `hysteresis` (optional non-negative `Float`), plus the shared
/// managed set: `max_shelve_ticks`, `priority`, `class`, and
/// `response_ticks` (required non-negative `Int`s).
/// `rationalization` is the decision-70 record's required prose half —
/// a typed argument so a composed plant cannot omit it.
pub struct ManagedLatchingAlarmSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
    /// Which managed inputs the instance declares.
    pub managed: ManagedInputs,
    /// The instance's decision-70 rationalization record.
    pub rationalization: Rationalization,
}

/// Typed port handles for a `managed-latching-alarm` instance.
pub struct ManagedLatchingAlarmInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `in` port (`In`, `Float`): the monitored analog value.
    pub input: Sink<f64>,
    /// `ack` port (`In`, `Bool`): the operator's clearing command.
    pub ack: Sink<bool>,
    /// The managed-alarm ports — optional inputs and the uniform
    /// status outputs.
    pub managed: ManagedAlarmHandles,
    /// `alarm` port (`Out`, `Bool`): the limit-violation state.
    pub alarm: Source<bool>,
    /// `unacknowledged` port (`Out`, `Bool`): the
    /// trip-until-acknowledged latch.
    pub unacknowledged: Source<bool>,
}

impl ManagedLatchingAlarmSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "managed-latching-alarm";

    /// The declared parameter set — the sibling's limits plus the
    /// managed configuration.
    pub const PARAMETERS: &'static [ParamDecl] = &[
        required("low_limit", ValueKind::Float, Some(FINITE_F64)),
        required("high_limit", ValueKind::Float, Some(FINITE_F64)),
        optional("hysteresis", ValueKind::Float, Some(NONNEGATIVE_F64)),
        required("max_shelve_ticks", ValueKind::Int, Some(NONNEGATIVE_INT)),
        required("priority", ValueKind::Int, Some(NONNEGATIVE_INT)),
        required("class", ValueKind::Int, Some(NONNEGATIVE_INT)),
        required("response_ticks", ValueKind::Int, Some(NONNEGATIVE_INT)),
    ];

    /// A spec carrying `parameters` as the instance's parameter map;
    /// `managed` selects which managed inputs the instance declares;
    /// `rationalization` is the instance's required decision-70 record.
    pub fn new(
        parameters: Parameters,
        managed: ManagedInputs,
        rationalization: Rationalization,
    ) -> Self {
        Self {
            parameters,
            managed,
            rationalization,
        }
    }
}

impl Spec for ManagedLatchingAlarmSpec {
    type Instance = ManagedLatchingAlarmInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        let mut ports = vec![
            port("in", Direction::In, ValueKind::Float),
            port("ack", Direction::In, ValueKind::Bool),
        ];
        ports.extend(managed_input_ports(self.managed));
        ports.extend([
            port("alarm", Direction::Out, ValueKind::Bool),
            port("unacknowledged", Direction::Out, ValueKind::Bool),
        ]);
        ports.extend(managed_output_ports());
        ports
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn rationalization(&self) -> Option<&Rationalization> {
        Some(&self.rationalization)
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        ManagedLatchingAlarmInstance {
            id,
            input: Sink::port(id, "in"),
            ack: Sink::port(id, "ack"),
            managed: ManagedAlarmHandles::for_component(id, self.managed),
            alarm: Source::port(id, "alarm"),
            unacknowledged: Source::port(id, "unacknowledged"),
        }
    }
}

/// Spec for the `managed-bool-latching-alarm` kind:
/// `bool-latching-alarm`'s two-flag lifecycle for Bool-sourced
/// conditions plus the shelving, suppression, and out-of-service
/// lifecycle decisions 71–73 record.
///
/// Ports mirror the descriptor: `in` (`In`, `Bool`), `ack` (`In`,
/// `Bool`), the spec-declared subset of `shelve`/`oos`/`suppress`
/// (`In`, `Bool`), then `alarm`, `unacknowledged`, `shelved`,
/// `suppressed`, `out_of_service` (all `Out`, `Bool`). Parameters — the
/// shared managed set: `max_shelve_ticks`, `priority`, `class`, and
/// `response_ticks` (required non-negative `Int`s); the Bool analogue
/// of the standing limit state has no limits or hysteresis.
/// `rationalization` is the decision-70 record's required prose half —
/// a typed argument so a composed plant cannot omit it.
pub struct ManagedBoolLatchingAlarmSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
    /// Which managed inputs the instance declares.
    pub managed: ManagedInputs,
    /// The instance's decision-70 rationalization record.
    pub rationalization: Rationalization,
}

/// Typed port handles for a `managed-bool-latching-alarm` instance.
pub struct ManagedBoolLatchingAlarmInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `in` port (`In`, `Bool`): the Bool alarm condition.
    pub input: Sink<bool>,
    /// `ack` port (`In`, `Bool`): the operator's clearing command.
    pub ack: Sink<bool>,
    /// The managed-alarm ports — optional inputs and the uniform
    /// status outputs.
    pub managed: ManagedAlarmHandles,
    /// `alarm` port (`Out`, `Bool`): the standing condition state.
    pub alarm: Source<bool>,
    /// `unacknowledged` port (`Out`, `Bool`): the
    /// asserted-until-acknowledged latch.
    pub unacknowledged: Source<bool>,
}

impl ManagedBoolLatchingAlarmSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "managed-bool-latching-alarm";

    /// The declared parameter set — the shared managed configuration.
    pub const PARAMETERS: &'static [ParamDecl] = MANAGED_ALARM_PARAMETERS;

    /// A spec carrying `parameters` as the instance's parameter map;
    /// `managed` selects which managed inputs the instance declares;
    /// `rationalization` is the instance's required decision-70 record.
    pub fn new(
        parameters: Parameters,
        managed: ManagedInputs,
        rationalization: Rationalization,
    ) -> Self {
        Self {
            parameters,
            managed,
            rationalization,
        }
    }
}

impl Spec for ManagedBoolLatchingAlarmSpec {
    type Instance = ManagedBoolLatchingAlarmInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        let mut ports = vec![
            port("in", Direction::In, ValueKind::Bool),
            port("ack", Direction::In, ValueKind::Bool),
        ];
        ports.extend(managed_input_ports(self.managed));
        ports.extend([
            port("alarm", Direction::Out, ValueKind::Bool),
            port("unacknowledged", Direction::Out, ValueKind::Bool),
        ]);
        ports.extend(managed_output_ports());
        ports
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn rationalization(&self) -> Option<&Rationalization> {
        Some(&self.rationalization)
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        ManagedBoolLatchingAlarmInstance {
            id,
            input: Sink::port(id, "in"),
            ack: Sink::port(id, "ack"),
            managed: ManagedAlarmHandles::for_component(id, self.managed),
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

/// Spec for the `bool-gate` kind: folding a declared `in_1`…`in_N`
/// boolean input set through the `operation`'s truth function.
///
/// The port set is not static: an instance declares
/// [`inputs`](Self::inputs) gate inputs, and [`ports`](Spec::ports)
/// emits `in_1`…`in_N` (`In`, `Bool`) — each connected through the
/// instance's [`input`](BoolGateInstance::input) handle — followed by
/// `out` (`Out`, `Bool`). Mirrors the descriptor: the `in_N` set, then
/// `out`.
///
/// Parameters: `operation` (required `Int` in `0..=2` — the
/// `GateOperation` code).
pub struct BoolGateSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
    /// How many `in_N` gate inputs the instance declares.
    pub inputs: usize,
}

/// Typed port handles for a `bool-gate` instance.
pub struct BoolGateInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `out` port (`Out`, `Bool`): the folded truth-function result.
    pub out: Source<bool>,
}

impl BoolGateInstance {
    /// The `index`th gate input (`in_1`…`in_N`, where `N` is the spec's
    /// [`inputs`](BoolGateSpec::inputs)): an `In`, `Bool` port. The
    /// input count lives on the spec, not on this handle — an `index`
    /// outside `1..=N` names a port the instance does not declare, and
    /// [`build`](crate::PlantBuilder::build) reports the connection.
    pub fn input(&self, index: usize) -> Sink<bool> {
        Sink::port(self.id, &format!("in_{index}"))
    }
}

impl BoolGateSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "bool-gate";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] =
        &[required("operation", ValueKind::Int, Some(CODE_RANGE))];

    /// A spec for an instance declaring `inputs` gate inputs and
    /// carrying `parameters` as its parameter map.
    pub fn new(parameters: Parameters, inputs: usize) -> Self {
        Self { parameters, inputs }
    }
}

impl Spec for BoolGateSpec {
    type Instance = BoolGateInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        let mut ports: Vec<_> = (1..=self.inputs)
            .map(|index| port(&format!("in_{index}"), Direction::In, ValueKind::Bool))
            .collect();
        ports.push(port("out", Direction::Out, ValueKind::Bool));
        ports
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        BoolGateInstance {
            id,
            out: Source::port(id, "out"),
        }
    }
}

/// Spec for the `pump-group` kind: an N-pump duty/standby group with a
/// declared rotation policy, lag staging on unmet demand, availability
/// exclusion, and automatic duty handover on failed feedback.
///
/// The port set is not static: an instance declares
/// [`pumps`](Self::pumps) managed pumps, and [`ports`](Spec::ports)
/// emits `demand` (`In`, `Int`), then `cmd_i` (`Out`, `Bool`),
/// `run_i` (`In`, `Bool`), `fault_i` (`In`, `Bool`), `avail_i` (`In`,
/// `Bool`) per pump — each connected through the instance's
/// [`cmd`](PumpGroupInstance::cmd), [`run`](PumpGroupInstance::run),
/// [`fault`](PumpGroupInstance::fault), and
/// [`avail`](PumpGroupInstance::avail) handles — followed by `duty`
/// (`Out`, `Int`), `staged` (`Out`, `Int`), `none_available` (`Out`,
/// `Bool`), and `all_faulted` (`Out`, `Bool`). Mirrors the descriptor's
/// `io_requirements` order.
///
/// Parameters: `rotation` (required `Int` in `0..=2` — the
/// `RotationPolicy` code: `0` alternate each pump-down cycle, `1`
/// timed interval, `2` least-run-hours first); `rotation_ticks`
/// (optional positive `Int`, required by the kind when `rotation` is
/// `1` — the conditional requirement is the constructor's, the spec
/// declares it optional); `start_delay_ticks`, `restage_delay_ticks`,
/// `min_off_ticks` (optional non-negative `Int`s).
pub struct PumpGroupSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
    /// How many pumps the instance manages — the `cmd_i`/`run_i`/
    /// `fault_i`/`avail_i` port families' index bound.
    pub pumps: usize,
}

/// Typed port handles for a `pump-group` instance.
pub struct PumpGroupInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `demand` port (`In`, `Int`): the requested stage count — `0`
    /// all stopped, `1` duty only, `2` duty plus lag.
    pub demand: Sink<i64>,
    /// `duty` port (`Out`, `Int`): the 1-based index of the pump
    /// holding duty, `0` while none holds it.
    pub duty: Source<i64>,
    /// `staged` port (`Out`, `Int`): how many pumps the group currently
    /// commands.
    pub staged: Source<i64>,
    /// `none_available` port (`Out`, `Bool`): no pump is available.
    pub none_available: Source<bool>,
    /// `all_faulted` port (`Out`, `Bool`): every pump's `fault_i`
    /// reads failed.
    pub all_faulted: Source<bool>,
}

impl PumpGroupInstance {
    /// Pump `index`'s run request (`cmd_1`…`cmd_N`, where `N` is the
    /// spec's [`pumps`](PumpGroupSpec::pumps)): an `Out`, `Bool` port.
    /// The pump count lives on the spec, not on this handle — an
    /// `index` outside `1..=N` names a port the instance does not
    /// declare, and [`build`](crate::PlantBuilder::build) reports the
    /// connection.
    pub fn cmd(&self, index: usize) -> Source<bool> {
        Source::port(self.id, &format!("cmd_{index}"))
    }

    /// Pump `index`'s run feedback (`run_1`…`run_N`): an `In`, `Bool`
    /// port.
    pub fn run(&self, index: usize) -> Sink<bool> {
        Sink::port(self.id, &format!("run_{index}"))
    }

    /// Pump `index`'s proven-failure flag (`fault_1`…`fault_N`): an
    /// `In`, `Bool` port.
    pub fn fault(&self, index: usize) -> Sink<bool> {
        Sink::port(self.id, &format!("fault_{index}"))
    }

    /// Pump `index`'s aggregated availability (`avail_1`…`avail_N`): an
    /// `In`, `Bool` port.
    pub fn avail(&self, index: usize) -> Sink<bool> {
        Sink::port(self.id, &format!("avail_{index}"))
    }
}

impl PumpGroupSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "pump-group";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] = &[
        required("rotation", ValueKind::Int, Some(CODE_RANGE)),
        optional("rotation_ticks", ValueKind::Int, Some(POSITIVE_INT)),
        optional("start_delay_ticks", ValueKind::Int, Some(NONNEGATIVE_INT)),
        optional("restage_delay_ticks", ValueKind::Int, Some(NONNEGATIVE_INT)),
        optional("min_off_ticks", ValueKind::Int, Some(NONNEGATIVE_INT)),
    ];

    /// A spec for an instance managing `pumps` pumps and carrying
    /// `parameters` as its parameter map.
    pub fn new(parameters: Parameters, pumps: usize) -> Self {
        Self { parameters, pumps }
    }
}

impl Spec for PumpGroupSpec {
    type Instance = PumpGroupInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        let mut ports = vec![port("demand", Direction::In, ValueKind::Int)];
        for index in 1..=self.pumps {
            ports.push(port(
                &format!("cmd_{index}"),
                Direction::Out,
                ValueKind::Bool,
            ));
            ports.push(port(
                &format!("run_{index}"),
                Direction::In,
                ValueKind::Bool,
            ));
            ports.push(port(
                &format!("fault_{index}"),
                Direction::In,
                ValueKind::Bool,
            ));
            ports.push(port(
                &format!("avail_{index}"),
                Direction::In,
                ValueKind::Bool,
            ));
        }
        ports.push(port("duty", Direction::Out, ValueKind::Int));
        ports.push(port("staged", Direction::Out, ValueKind::Int));
        ports.push(port("none_available", Direction::Out, ValueKind::Bool));
        ports.push(port("all_faulted", Direction::Out, ValueKind::Bool));
        ports
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        PumpGroupInstance {
            id,
            demand: Sink::port(id, "demand"),
            duty: Source::port(id, "duty"),
            staged: Source::port(id, "staged"),
            none_available: Source::port(id, "none_available"),
            all_faulted: Source::port(id, "all_faulted"),
        }
    }
}

/// Spec for the `blower-group` kind: an N-blower capacity-staged group
/// with per-unit declared bounds, start-interval protection, the
/// vent-based join/departure choreography, and the declared rotation
/// and staging-authority policies.
///
/// The port set is not static: an instance declares
/// [`blowers`](Self::blowers) managed blowers, and
/// [`ports`](Spec::ports) emits `demand` (`In`, `Float`), then
/// `approve` (`In`, `Bool`) when the spec's [`approve`](Self::approve)
/// flag wires the operator release — required when `staging_authority`
/// is `1` — then `cmd_i` (`Out`, `Bool`), `run_i` (`In`, `Bool`),
/// `fault_i` (`In`, `Bool`), `avail_i` (`In`, `Bool`), `capacity_i`
/// (`Out`, `Float`), `vent_i` (`Out`, `Bool`) per blower — each
/// connected through the instance's [`cmd`](BlowerGroupInstance::cmd),
/// [`run`](BlowerGroupInstance::run), [`fault`](BlowerGroupInstance::fault),
/// [`avail`](BlowerGroupInstance::avail),
/// [`capacity`](BlowerGroupInstance::capacity), and
/// [`vent`](BlowerGroupInstance::vent) handles — followed by `staged`
/// (`Out`, `Int`), `none_available`/`all_faulted`/`staging_pending`/
/// `transition` (`Out`, `Bool`). Mirrors the descriptor's
/// `io_requirements` order.
///
/// The parameter set is indexed by `blowers` — `unit_<i>_min_flow`/
/// `unit_<i>_max_flow`/`unit_<i>_max_current` per unit — a different
/// key set per instance, so `declared_parameters` is `None` and
/// `build` leaves the map to the kind's `from_parameters`, which
/// reports `Missing`/`Invalid` naming the offending key. The shared
/// keys are `staging_authority` (required `Int` in `0..=2` — `0`
/// automatic, `1` operator-approval, `2` flag-only), `stage_up`/
/// `stage_down` (required non-negative `Float`s), `min_run_ticks`/
/// `min_start_interval_ticks` (required non-negative `Int`s),
/// `vent_ticks` (required positive `Int`), and `rotation` (required
/// `Int` in `0..=2` — `0` none, `1` equalize runtime, `2` fixed
/// order).
pub struct BlowerGroupSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
    /// How many blowers the instance manages — the `cmd_i`/`run_i`/
    /// `fault_i`/`avail_i`/`capacity_i`/`vent_i` port families' index
    /// bound.
    pub blowers: usize,
    /// Whether the instance declares the `approve` port — the
    /// operator release the `staging_authority = 1` instance requires;
    /// a plant running automatic or flag-only staging leaves it
    /// unwired.
    pub approve: bool,
}

/// Typed port handles for a `blower-group` instance.
pub struct BlowerGroupInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `demand` port (`In`, `Float`): the aggregate capacity demand —
    /// the header-coordinator's `blower_demand`.
    pub demand: Sink<f64>,
    /// `approve` port (`In`, `Bool`): the operator release — `Some`
    /// where the spec's `approve` flag declares it.
    pub approve: Option<Sink<bool>>,
    /// `staged` port (`Out`, `Int`): how many blowers the group
    /// currently commands.
    pub staged: Source<i64>,
    /// `none_available` port (`Out`, `Bool`): no blower is available.
    pub none_available: Source<bool>,
    /// `all_faulted` port (`Out`, `Bool`): every blower's `fault_i`
    /// reads failed.
    pub all_faulted: Source<bool>,
    /// `staging_pending` port (`Out`, `Bool`): a stage change standing
    /// unexecuted under a non-automatic authority.
    pub staging_pending: Source<bool>,
    /// `transition` port (`Out`, `Bool`): a join, departure, or
    /// rotation handover in progress — the valve-freeze surface.
    pub transition: Source<bool>,
}

impl BlowerGroupInstance {
    /// Blower `index`'s run request (`cmd_1`…`cmd_N`, where `N` is the
    /// spec's [`blowers`](BlowerGroupSpec::blowers)): an `Out`, `Bool`
    /// port. An `index` outside `1..=N` names a port the instance does
    /// not declare, and [`build`](crate::PlantBuilder::build) reports
    /// the connection.
    pub fn cmd(&self, index: usize) -> Source<bool> {
        Source::port(self.id, &format!("cmd_{index}"))
    }

    /// Blower `index`'s run feedback (`run_1`…`run_N`): an `In`,
    /// `Bool` port.
    pub fn run(&self, index: usize) -> Sink<bool> {
        Sink::port(self.id, &format!("run_{index}"))
    }

    /// Blower `index`'s proven-failure flag (`fault_1`…`fault_N`): an
    /// `In`, `Bool` port.
    pub fn fault(&self, index: usize) -> Sink<bool> {
        Sink::port(self.id, &format!("fault_{index}"))
    }

    /// Blower `index`'s aggregated availability (`avail_1`…`avail_N`):
    /// an `In`, `Bool` port.
    pub fn avail(&self, index: usize) -> Sink<bool> {
        Sink::port(self.id, &format!("avail_{index}"))
    }

    /// Blower `index`'s capacity demand (`capacity_1`…`capacity_N`):
    /// an `Out`, `Float` port — the unit's share under the equal
    /// split, clamped to its declared bounds.
    pub fn capacity(&self, index: usize) -> Source<f64> {
        Source::port(self.id, &format!("capacity_{index}"))
    }

    /// Blower `index`'s vent command (`vent_1`…`vent_N`): an `Out`,
    /// `Bool` port — `true` holds the unit off the header.
    pub fn vent(&self, index: usize) -> Source<bool> {
        Source::port(self.id, &format!("vent_{index}"))
    }
}

impl BlowerGroupSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "blower-group";

    /// A spec for an instance managing `blowers` blowers, carrying
    /// `parameters` as its parameter map, and declaring the `approve`
    /// port when `approve` is set.
    pub fn new(parameters: Parameters, blowers: usize, approve: bool) -> Self {
        Self {
            parameters,
            blowers,
            approve,
        }
    }
}

impl Spec for BlowerGroupSpec {
    type Instance = BlowerGroupInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        let mut ports = vec![port("demand", Direction::In, ValueKind::Float)];
        if self.approve {
            ports.push(port("approve", Direction::In, ValueKind::Bool));
        }
        for index in 1..=self.blowers {
            ports.push(port(
                &format!("cmd_{index}"),
                Direction::Out,
                ValueKind::Bool,
            ));
            ports.push(port(
                &format!("run_{index}"),
                Direction::In,
                ValueKind::Bool,
            ));
            ports.push(port(
                &format!("fault_{index}"),
                Direction::In,
                ValueKind::Bool,
            ));
            ports.push(port(
                &format!("avail_{index}"),
                Direction::In,
                ValueKind::Bool,
            ));
            ports.push(port(
                &format!("capacity_{index}"),
                Direction::Out,
                ValueKind::Float,
            ));
            ports.push(port(
                &format!("vent_{index}"),
                Direction::Out,
                ValueKind::Bool,
            ));
        }
        ports.push(port("staged", Direction::Out, ValueKind::Int));
        ports.push(port("none_available", Direction::Out, ValueKind::Bool));
        ports.push(port("all_faulted", Direction::Out, ValueKind::Bool));
        ports.push(port("staging_pending", Direction::Out, ValueKind::Bool));
        ports.push(port("transition", Direction::Out, ValueKind::Bool));
        ports
    }

    /// The parameter set is indexed by `blowers` — not statically
    /// enumerable, so the map goes to the kind's `from_parameters`
    /// unchecked by `build`.
    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        None
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        BlowerGroupInstance {
            id,
            demand: Sink::port(id, "demand"),
            approve: self.approve.then(|| Sink::port(id, "approve")),
            staged: Source::port(id, "staged"),
            none_available: Source::port(id, "none_available"),
            all_faulted: Source::port(id, "all_faulted"),
            staging_pending: Source::port(id, "staging_pending"),
            transition: Source::port(id, "transition"),
        }
    }
}

/// Spec for the `sr-latch` kind: a reset-dominant set/reset bistable.
///
/// Ports mirror the descriptor: `set` (`In`, `Bool`), `reset` (`In`,
/// `Bool`), `out` (`Out`, `Bool`). The kind takes no parameters.
pub struct SrLatchSpec {
    /// The instance's parameter map — the kind declares no parameters,
    /// so any key is an [`UnknownParameter`](crate::BuildError::UnknownParameter)
    /// at `build`.
    pub parameters: Parameters,
}

/// Typed port handles for an `sr-latch` instance.
pub struct SrLatchInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `set` port (`In`, `Bool`): the latching condition.
    pub set: Sink<bool>,
    /// `reset` port (`In`, `Bool`): the clearing condition; dominant
    /// when both inputs assert.
    pub reset: Sink<bool>,
    /// `out` port (`Out`, `Bool`): the latched state.
    pub out: Source<bool>,
}

impl SrLatchSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "sr-latch";

    /// The declared parameter set: the kind takes none.
    pub const PARAMETERS: &'static [ParamDecl] = &[];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for SrLatchSpec {
    type Instance = SrLatchInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("set", Direction::In, ValueKind::Bool),
            port("reset", Direction::In, ValueKind::Bool),
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
        SrLatchInstance {
            id,
            set: Sink::port(id, "set"),
            reset: Sink::port(id, "reset"),
            out: Source::port(id, "out"),
        }
    }
}

/// Spec for the `edge-trigger` kind: emitting a one-tick pulse on a
/// selected edge of a boolean input.
///
/// Ports mirror the descriptor: `in` (`In`, `Bool`), `out` (`Out`,
/// `Bool`). Parameters: `edge` (required `Int` in `0..=2` — the `Edge`
/// code).
pub struct EdgeTriggerSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
}

/// Typed port handles for an `edge-trigger` instance.
pub struct EdgeTriggerInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `in` port (`In`, `Bool`): the observed signal.
    pub input: Sink<bool>,
    /// `out` port (`Out`, `Bool`): the one-tick pulse.
    pub out: Source<bool>,
}

impl EdgeTriggerSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "edge-trigger";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] =
        &[required("edge", ValueKind::Int, Some(CODE_RANGE))];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for EdgeTriggerSpec {
    type Instance = EdgeTriggerInstance;

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
        EdgeTriggerInstance {
            id,
            input: Sink::port(id, "in"),
            out: Source::port(id, "out"),
        }
    }
}

/// Spec for the `threshold-chain` kind: the station's ordered
/// start/stop setpoint table driving a pump-stage demand.
///
/// Ports mirror the descriptor: `level` (`In`, `Float`); `demand`
/// (`Out`, `Int`); `duty_call`, `lag_call`, `below_cutoff`,
/// `high_level` (`Out`, `Bool`). Parameters: `cutoff`, `stop`,
/// `start`, `lag_start`, `high` (required finite `Float`s; their
/// strictly-increasing ordering is a cross-parameter invariant the
/// kind's `from_parameters` checks — a spec cannot express it) and
/// `on_bad_demand` (required `Int` in `0..=2`).
pub struct ThresholdChainSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
}

/// Typed port handles for a `threshold-chain` instance.
pub struct ThresholdChainInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `level` port (`In`, `Float`): the station level measurement —
    /// normally the `failover-select`'s chosen source.
    pub level: Sink<f64>,
    /// `demand` port (`Out`, `Int`): the held stage count — `0` all
    /// stopped, `1` duty called, `2` duty plus lag.
    pub demand: Source<i64>,
    /// `duty_call` port (`Out`, `Bool`): asserts while the held demand
    /// is at least `1`.
    pub duty_call: Source<bool>,
    /// `lag_call` port (`Out`, `Bool`): asserts while the held demand
    /// is `2`.
    pub lag_call: Source<bool>,
    /// `below_cutoff` port (`Out`, `Bool`): the low-water cut-off
    /// condition.
    pub below_cutoff: Source<bool>,
    /// `high_level` port (`Out`, `Bool`): the high-level alarm
    /// condition.
    pub high_level: Source<bool>,
}

impl ThresholdChainSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "threshold-chain";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] = &[
        required("cutoff", ValueKind::Float, Some(FINITE_F64)),
        required("stop", ValueKind::Float, Some(FINITE_F64)),
        required("start", ValueKind::Float, Some(FINITE_F64)),
        required("lag_start", ValueKind::Float, Some(FINITE_F64)),
        required("high", ValueKind::Float, Some(FINITE_F64)),
        required("on_bad_demand", ValueKind::Int, Some(CODE_RANGE)),
    ];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for ThresholdChainSpec {
    type Instance = ThresholdChainInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("level", Direction::In, ValueKind::Float),
            port("demand", Direction::Out, ValueKind::Int),
            port("duty_call", Direction::Out, ValueKind::Bool),
            port("lag_call", Direction::Out, ValueKind::Bool),
            port("below_cutoff", Direction::Out, ValueKind::Bool),
            port("high_level", Direction::Out, ValueKind::Bool),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        ThresholdChainInstance {
            id,
            level: Sink::port(id, "level"),
            demand: Source::port(id, "demand"),
            duty_call: Source::port(id, "duty_call"),
            lag_call: Source::port(id, "lag_call"),
            below_cutoff: Source::port(id, "below_cutoff"),
            high_level: Source::port(id, "high_level"),
        }
    }
}

/// Spec for the `failover-select` kind: quality-driven selection
/// between a primary and a backup analog measurement.
///
/// Ports mirror the descriptor: `primary` (`In`, `Float`), `backup`
/// (`In`, `Float`), `out` (`Out`, `Float`), `backup_active` (`Out`,
/// `Bool`). The kind takes no parameters.
pub struct FailoverSelectSpec {
    /// The instance's parameter map — the kind declares no parameters,
    /// so any key is an [`UnknownParameter`](crate::BuildError::UnknownParameter)
    /// at `build`.
    pub parameters: Parameters,
}

/// Typed port handles for a `failover-select` instance.
pub struct FailoverSelectInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `primary` port (`In`, `Float`): the preferred measurement.
    pub primary: Sink<f64>,
    /// `backup` port (`In`, `Float`): the measurement served while the
    /// primary is unusable.
    pub backup: Sink<f64>,
    /// `out` port (`Out`, `Float`): the selected source's sample.
    pub out: Source<f64>,
    /// `backup_active` port (`Out`, `Bool`): asserts while the backup
    /// is selected.
    pub backup_active: Source<bool>,
}

impl FailoverSelectSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "failover-select";

    /// The declared parameter set: the kind takes none.
    pub const PARAMETERS: &'static [ParamDecl] = &[];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for FailoverSelectSpec {
    type Instance = FailoverSelectInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("primary", Direction::In, ValueKind::Float),
            port("backup", Direction::In, ValueKind::Float),
            port("out", Direction::Out, ValueKind::Float),
            port("backup_active", Direction::Out, ValueKind::Bool),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        FailoverSelectInstance {
            id,
            primary: Sink::port(id, "primary"),
            backup: Sink::port(id, "backup"),
            out: Source::port(id, "out"),
            backup_active: Source::port(id, "backup_active"),
        }
    }
}

/// Spec for the `flow-paced-ratio` kind: the chemical-dosing
/// `dose × flow` demand — optional analyzer `trim`, declared dose and
/// rate bounds, and declared responses to untrusted inputs.
///
/// Ports mirror the descriptor: `flow` (`In`, `Float`) — the measured
/// process flow; `dose` (`In`, `Float`) — the operator dose setpoint,
/// conventionally wired to a writable internal `In` point; `trim`
/// (`In`, `Float`) — the optional analyzer correction, declared only
/// when the spec's `trim` flag is set; `demand` (`Out`, `Float`);
/// `clamped` and `fallback_active` (`Out`, `Bool`). Parameters:
/// `min_dose`, `max_dose`, `min_rate`, `max_rate`, `fallback_rate`
/// (required finite `Float`s — the `min <= max` ordering of each bound
/// pair is a cross-parameter invariant the kind's `from_parameters`
/// checks; a spec cannot express it), `on_bad_flow` (required `Int` in
/// `0..=2`), and `on_bad_trim` (required `Int` in `0..=1`).
pub struct FlowPacedRatioSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
    /// Whether the instance declares the optional `trim` port. `false`
    /// emits an instance with no `trim` to wire — the kind paces
    /// untrimmed, the port's absence being the "unwired means unity"
    /// rule.
    pub trim: bool,
}

/// Typed port handles for a `flow-paced-ratio` instance.
pub struct FlowPacedRatioInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `flow` port (`In`, `Float`): the measured process flow — the
    /// pacing signal.
    pub flow: Sink<f64>,
    /// `dose` port (`In`, `Float`): the operator dose setpoint per flow
    /// unit — wire it to a writable internal `In` point so writes ride
    /// the journaled receipted path.
    pub dose: Sink<f64>,
    /// `trim` port (`In`, `Float`): the analyzer correction —
    /// `Some` only when the spec declared the port; wiring a handle
    /// the emitted instance does not carry is `UnknownPort` at `build`.
    pub trim: Option<Sink<f64>>,
    /// `demand` port (`Out`, `Float`): the bounded actuator demand.
    pub demand: Source<f64>,
    /// `clamped` port (`Out`, `Bool`): asserts while the dose or rate
    /// bound engages.
    pub clamped: Source<bool>,
    /// `fallback_active` port (`Out`, `Bool`): asserts while a declared
    /// bad-signal response runs.
    pub fallback_active: Source<bool>,
}

impl FlowPacedRatioSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "flow-paced-ratio";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] = &[
        required("min_dose", ValueKind::Float, Some(FINITE_F64)),
        required("max_dose", ValueKind::Float, Some(FINITE_F64)),
        required("min_rate", ValueKind::Float, Some(FINITE_F64)),
        required("max_rate", ValueKind::Float, Some(FINITE_F64)),
        required("on_bad_flow", ValueKind::Int, Some(CODE_RANGE)),
        required("fallback_rate", ValueKind::Float, Some(FINITE_F64)),
        required("on_bad_trim", ValueKind::Int, Some(BINARY_CODE_RANGE)),
    ];

    /// A spec carrying `parameters` as the instance's parameter map;
    /// `trim` selects whether the optional analyzer port is declared.
    pub fn new(parameters: Parameters, trim: bool) -> Self {
        Self { parameters, trim }
    }
}

impl Spec for FlowPacedRatioSpec {
    type Instance = FlowPacedRatioInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        let mut ports = vec![
            port("flow", Direction::In, ValueKind::Float),
            port("dose", Direction::In, ValueKind::Float),
        ];
        if self.trim {
            ports.push(port("trim", Direction::In, ValueKind::Float));
        }
        ports.extend([
            port("demand", Direction::Out, ValueKind::Float),
            port("clamped", Direction::Out, ValueKind::Bool),
            port("fallback_active", Direction::Out, ValueKind::Bool),
        ]);
        ports
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        FlowPacedRatioInstance {
            id,
            flow: Sink::port(id, "flow"),
            dose: Sink::port(id, "dose"),
            trim: self.trim.then(|| Sink::port(id, "trim")),
            demand: Source::port(id, "demand"),
            clamped: Source::port(id, "clamped"),
            fallback_active: Source::port(id, "fallback_active"),
        }
    }
}

/// Spec for the `deviation-monitor` kind: the dose-confirmation check
/// architecture decision 53 records — the commanded and measured
/// chemical rates or totals accumulated over a declared window, the
/// window's relative deviation tripping `deviating` past the declared
/// limit.
///
/// Ports mirror the descriptor: `expected` (`In`, `Float`) — the
/// commanded rate or total; `measured` (`In`, `Float`) — the measured
/// consumption; `deviation` (`Out`, `Float`) — the last completed
/// window's relative deviation; `deviating` (`Out`, `Bool`) — the
/// dose-not-confirmed condition the alarm set consumes. Parameters:
/// `deviation_limit` (required non-negative finite `Float`) and
/// `window_ticks` (required `Int` in `1..=i64::MAX`).
pub struct DeviationMonitorSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
}

/// Typed port handles for a `deviation-monitor` instance.
pub struct DeviationMonitorInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `expected` port (`In`, `Float`): the commanded chemical rate or
    /// total — conventionally the ratio kind's `demand` or a
    /// `totalizer`'s commanded total.
    pub expected: Sink<f64>,
    /// `measured` port (`In`, `Float`): the measured consumption —
    /// a discharge flow or drawdown-derived rate, or a measured total.
    pub measured: Sink<f64>,
    /// `deviation` port (`Out`, `Float`): the last completed window's
    /// relative deviation.
    pub deviation: Source<f64>,
    /// `deviating` port (`Out`, `Bool`): the dose-not-confirmed
    /// condition — wire it into a `bool-latching-alarm` for the skid's
    /// alarm set.
    pub deviating: Source<bool>,
}

impl DeviationMonitorSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "deviation-monitor";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] = &[
        required("deviation_limit", ValueKind::Float, Some(NONNEGATIVE_F64)),
        required("window_ticks", ValueKind::Int, Some(POSITIVE_INT)),
    ];

    /// A spec carrying `parameters` as the instance's parameter map.
    pub fn new(parameters: Parameters) -> Self {
        Self { parameters }
    }
}

impl Spec for DeviationMonitorSpec {
    type Instance = DeviationMonitorInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        vec![
            port("expected", Direction::In, ValueKind::Float),
            port("measured", Direction::In, ValueKind::Float),
            port("deviation", Direction::Out, ValueKind::Float),
            port("deviating", Direction::Out, ValueKind::Bool),
        ]
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        DeviationMonitorInstance {
            id,
            expected: Sink::port(id, "expected"),
            measured: Sink::port(id, "measured"),
            deviation: Source::port(id, "deviation"),
            deviating: Source::port(id, "deviating"),
        }
    }
}

/// Spec for the `backwash-coordinator` kind: shared-supply arbitration
/// across a filter bank — an ordered request queue and an exclusive
/// held grant gated by the declared permissives, with a declared queue
/// policy and an optional operator reorder input.
///
/// The port set is not static: an instance declares
/// [`filters`](Self::filters) managed filters, and
/// [`ports`](Spec::ports) emits `supply_ok`, `waste_ok`, `flow_ok`
/// (`In`, `Bool`) — the grant permissives aggregated upstream —
/// `reorder` (`In`, `Int`) when the spec's `reorder` flag wires the
/// operator's standing queue instruction — then `request_i` (`In`,
/// `Bool`), `grant_i` (`Out`, `Bool`), `position_i` (`Out`, `Int`) per
/// filter, each connected through the instance's
/// [`request`](BackwashCoordinatorInstance::request),
/// [`grant`](BackwashCoordinatorInstance::grant), and
/// [`position`](BackwashCoordinatorInstance::position) handles —
/// followed by `active` (`Out`, `Int`), `queued` (`Out`, `Int`), and
/// `resource_blocked` (`Out`, `Bool`). Mirrors the descriptor's
/// `io_requirements` order.
///
/// Parameters: `queue_policy` (required `Int` in `0..=2` — `0` FIFO,
/// `1` priority-by-trigger, `2` operator-managed) and `queued_state`
/// (required `Int` in `0..=1` — `0` keep filtering until granted, `1`
/// offline with standby cover). Both are the decision's
/// assumption-marked declared data — required, never defaulted.
pub struct BackwashCoordinatorSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
    /// How many filters the instance arbitrates — the
    /// `request_i`/`grant_i`/`position_i` port families' index bound.
    pub filters: usize,
    /// Whether the instance declares the `reorder` port — the
    /// operator's standing queue instruction, conventionally wired to
    /// a writable internal `In` point. `false` emits an instance with
    /// no `reorder` to wire — a plant not exposing reorder leaves the
    /// port unbound.
    pub reorder: bool,
}

/// Typed port handles for a `backwash-coordinator` instance.
pub struct BackwashCoordinatorInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `supply_ok` port (`In`, `Bool`): the supply-side grant
    /// permissive.
    pub supply_ok: Sink<bool>,
    /// `waste_ok` port (`In`, `Bool`): the waste-path grant
    /// permissive.
    pub waste_ok: Sink<bool>,
    /// `flow_ok` port (`In`, `Bool`): the shared-flow grant
    /// permissive.
    pub flow_ok: Sink<bool>,
    /// `reorder` port (`In`, `Int`): the operator's standing queue
    /// instruction — `Some` only when the spec declared the port;
    /// wire it to a writable internal `In` point so writes ride the
    /// journaled receipted path.
    pub reorder: Option<Sink<i64>>,
    /// `active` port (`Out`, `Int`): the 1-based index of the filter
    /// holding the grant, `0` while none does.
    pub active: Source<i64>,
    /// `queued` port (`Out`, `Int`): the count of requests pending in
    /// the queue.
    pub queued: Source<i64>,
    /// `resource_blocked` port (`Out`, `Bool`): asserts while a
    /// request stands first in queue and a grant permissive fails.
    pub resource_blocked: Source<bool>,
}

impl BackwashCoordinatorInstance {
    /// Filter `index`'s armed backwash request
    /// (`request_1`…`request_N`, where `N` is the spec's
    /// [`filters`](BackwashCoordinatorSpec::filters)): an `In`, `Bool`
    /// port. An `index` outside `1..=N` names a port the instance does
    /// not declare, and [`build`](crate::PlantBuilder::build) reports
    /// the connection.
    pub fn request(&self, index: usize) -> Sink<bool> {
        Sink::port(self.id, &format!("request_{index}"))
    }

    /// Filter `index`'s exclusive supply grant (`grant_1`…`grant_N`):
    /// an `Out`, `Bool` port.
    pub fn grant(&self, index: usize) -> Source<bool> {
        Source::port(self.id, &format!("grant_{index}"))
    }

    /// Filter `index`'s queue position (`position_1`…`position_N`):
    /// an `Out`, `Int` port — `0` while the filter is not queued, the
    /// grant holder included.
    pub fn position(&self, index: usize) -> Source<i64> {
        Source::port(self.id, &format!("position_{index}"))
    }
}

impl BackwashCoordinatorSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "backwash-coordinator";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] = &[
        required("queue_policy", ValueKind::Int, Some(CODE_RANGE)),
        required("queued_state", ValueKind::Int, Some(BINARY_CODE_RANGE)),
    ];

    /// A spec for an instance arbitrating `filters` filters, carrying
    /// `parameters` as its parameter map, and declaring the `reorder`
    /// port when `reorder` is set.
    pub fn new(parameters: Parameters, filters: usize, reorder: bool) -> Self {
        Self {
            parameters,
            filters,
            reorder,
        }
    }
}

impl Spec for BackwashCoordinatorSpec {
    type Instance = BackwashCoordinatorInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        let mut ports = vec![
            port("supply_ok", Direction::In, ValueKind::Bool),
            port("waste_ok", Direction::In, ValueKind::Bool),
            port("flow_ok", Direction::In, ValueKind::Bool),
        ];
        if self.reorder {
            ports.push(port("reorder", Direction::In, ValueKind::Int));
        }
        for index in 1..=self.filters {
            ports.push(port(
                &format!("request_{index}"),
                Direction::In,
                ValueKind::Bool,
            ));
            ports.push(port(
                &format!("grant_{index}"),
                Direction::Out,
                ValueKind::Bool,
            ));
            ports.push(port(
                &format!("position_{index}"),
                Direction::Out,
                ValueKind::Int,
            ));
        }
        ports.push(port("active", Direction::Out, ValueKind::Int));
        ports.push(port("queued", Direction::Out, ValueKind::Int));
        ports.push(port("resource_blocked", Direction::Out, ValueKind::Bool));
        ports
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        BackwashCoordinatorInstance {
            id,
            supply_ok: Sink::port(id, "supply_ok"),
            waste_ok: Sink::port(id, "waste_ok"),
            flow_ok: Sink::port(id, "flow_ok"),
            reorder: self.reorder.then(|| Sink::port(id, "reorder")),
            active: Source::port(id, "active"),
            queued: Source::port(id, "queued"),
            resource_blocked: Source::port(id, "resource_blocked"),
        }
    }
}

/// Spec for the `header-coordinator` kind: the shared aeration-header
/// coordination contract — the declared strategy, the bounded
/// set-point, the floored aggregate demand, and the capped pulse-grant
/// set.
///
/// The port set is not static: an instance declares
/// [`zones`](Self::zones) managed zones, and [`ports`](Spec::ports)
/// emits `pressure` (`In`, `Float`), then `valve_pos_i` (`In`,
/// `Float`), `airflow_i` (`In`, `Float`), `pulsing_i` (`In`, `Bool`),
/// `pulse_grant_i` (`Out`, `Bool`) per zone — each connected through
/// the instance's [`valve_pos`](HeaderCoordinatorInstance::valve_pos),
/// [`airflow`](HeaderCoordinatorInstance::airflow),
/// [`pulsing`](HeaderCoordinatorInstance::pulsing), and
/// [`pulse_grant`](HeaderCoordinatorInstance::pulse_grant) handles —
/// followed by `pressure_sp` (`Out`, `Float`), `blower_demand` (`Out`,
/// `Float`), `most_open` (`Out`, `Int`), `at_bound` (`Out`, `Bool`),
/// and `pulse_blocked` (`Out`, `Bool`). Mirrors the descriptor's
/// `io_requirements` order.
///
/// Parameters: `strategy` (required `Int` in `0..=2` — `0` constant
/// header pressure, `1` most-open-valve reset, `2` direct-airflow);
/// `pressure_hold`, `pressure_min`, `pressure_max`, `mov_band_lo`,
/// `mov_band_hi` (required finite `Float`s — `pressure_min <=
/// pressure_max` and `mov_band_lo <= mov_band_hi` are the
/// constructor's checks, the spec's ranges mirror the descriptor's);
/// `adjust_ticks` (required positive `Int`); `min_total_airflow`
/// (required non-negative `Float`); `max_pulsing` (required
/// non-negative `Int`). All nine are the decision's
/// assumption-marked declared data — required, never defaulted.
pub struct HeaderCoordinatorSpec {
    /// The instance's parameter map.
    pub parameters: Parameters,
    /// How many zones the instance coordinates — the
    /// `valve_pos_i`/`airflow_i`/`pulsing_i`/`pulse_grant_i` port
    /// families' index bound.
    pub zones: usize,
}

/// Typed port handles for a `header-coordinator` instance.
pub struct HeaderCoordinatorInstance {
    /// The allocated component id.
    pub id: ComponentId,
    /// `pressure` port (`In`, `Float`): the discharge-header pressure
    /// transmitter.
    pub pressure: Sink<f64>,
    /// `pressure_sp` port (`Out`, `Float`): the header-pressure
    /// set-point the blower capacity loop tracks.
    pub pressure_sp: Source<f64>,
    /// `blower_demand` port (`Out`, `Float`): the aggregate capacity
    /// demand the blower group stages against.
    pub blower_demand: Source<f64>,
    /// `most_open` port (`Out`, `Int`): the 1-based index of the zone
    /// whose valve is most open, `0` while none is trusted.
    pub most_open: Source<i64>,
    /// `at_bound` port (`Out`, `Bool`): the set-point or demand
    /// resting at a declared bound.
    pub at_bound: Source<bool>,
    /// `pulse_blocked` port (`Out`, `Bool`): a pulse request standing
    /// refused by the declared cap.
    pub pulse_blocked: Source<bool>,
}

impl HeaderCoordinatorInstance {
    /// Zone `index`'s valve-position feedback
    /// (`valve_pos_1`…`valve_pos_N`, where `N` is the spec's
    /// [`zones`](HeaderCoordinatorSpec::zones)): an `In`, `Float`
    /// port. An `index` outside `1..=N` names a port the instance does
    /// not declare, and [`build`](crate::PlantBuilder::build) reports
    /// the connection.
    pub fn valve_pos(&self, index: usize) -> Sink<f64> {
        Sink::port(self.id, &format!("valve_pos_{index}"))
    }

    /// Zone `index`'s airflow demand (`airflow_1`…`airflow_N`): an
    /// `In`, `Float` port — the DO/airflow loop's output the header
    /// must deliver.
    pub fn airflow(&self, index: usize) -> Sink<f64> {
        Sink::port(self.id, &format!("airflow_{index}"))
    }

    /// Zone `index`'s mixing-pulse request (`pulsing_1`…`pulsing_N`):
    /// an `In`, `Bool` port.
    pub fn pulsing(&self, index: usize) -> Sink<bool> {
        Sink::port(self.id, &format!("pulsing_{index}"))
    }

    /// Zone `index`'s pulse admission (`pulse_grant_1`…`pulse_grant_N`):
    /// an `Out`, `Bool` port.
    pub fn pulse_grant(&self, index: usize) -> Source<bool> {
        Source::port(self.id, &format!("pulse_grant_{index}"))
    }
}

impl HeaderCoordinatorSpec {
    /// The model kind string this spec emits.
    pub const KIND: &'static str = "header-coordinator";

    /// The declared parameter set.
    pub const PARAMETERS: &'static [ParamDecl] = &[
        required("strategy", ValueKind::Int, Some(CODE_RANGE)),
        required("pressure_hold", ValueKind::Float, Some(FINITE_F64)),
        required("pressure_min", ValueKind::Float, Some(FINITE_F64)),
        required("pressure_max", ValueKind::Float, Some(FINITE_F64)),
        required("mov_band_lo", ValueKind::Float, Some(FINITE_F64)),
        required("mov_band_hi", ValueKind::Float, Some(FINITE_F64)),
        required("adjust_ticks", ValueKind::Int, Some(POSITIVE_INT)),
        required("min_total_airflow", ValueKind::Float, Some(NONNEGATIVE_F64)),
        required("max_pulsing", ValueKind::Int, Some(NONNEGATIVE_INT)),
    ];

    /// A spec for an instance coordinating `zones` zones and carrying
    /// `parameters` as its parameter map.
    pub fn new(parameters: Parameters, zones: usize) -> Self {
        Self { parameters, zones }
    }
}

impl Spec for HeaderCoordinatorSpec {
    type Instance = HeaderCoordinatorInstance;

    fn kind(&self) -> &str {
        Self::KIND
    }

    fn ports(&self) -> Vec<PortDecl> {
        let mut ports = vec![port("pressure", Direction::In, ValueKind::Float)];
        for index in 1..=self.zones {
            ports.push(port(
                &format!("valve_pos_{index}"),
                Direction::In,
                ValueKind::Float,
            ));
            ports.push(port(
                &format!("airflow_{index}"),
                Direction::In,
                ValueKind::Float,
            ));
            ports.push(port(
                &format!("pulsing_{index}"),
                Direction::In,
                ValueKind::Bool,
            ));
            ports.push(port(
                &format!("pulse_grant_{index}"),
                Direction::Out,
                ValueKind::Bool,
            ));
        }
        ports.push(port("pressure_sp", Direction::Out, ValueKind::Float));
        ports.push(port("blower_demand", Direction::Out, ValueKind::Float));
        ports.push(port("most_open", Direction::Out, ValueKind::Int));
        ports.push(port("at_bound", Direction::Out, ValueKind::Bool));
        ports.push(port("pulse_blocked", Direction::Out, ValueKind::Bool));
        ports
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        Some(Self::PARAMETERS)
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        HeaderCoordinatorInstance {
            id,
            pressure: Sink::port(id, "pressure"),
            pressure_sp: Source::port(id, "pressure_sp"),
            blower_demand: Source::port(id, "blower_demand"),
            most_open: Source::port(id, "most_open"),
            at_bound: Source::port(id, "at_bound"),
            pulse_blocked: Source::port(id, "pulse_blocked"),
        }
    }
}
