//! The component contract: declared logical I/O plus a deterministic step.
//!
//! [`Component`] is what reusable control logic implements and what the
//! [`Executor`](crate::Executor) drives. Declarations are data —
//! [`IoRequirement`] values — so the executor can verify them against the
//! driver's point map before anything runs and scope each step's I/O to
//! exactly the declared set.

use dcs_core::{
    CommandError, ComponentDescriptor, Direction, EmittedEvent, IoDriver, IoError, PointId,
    PointType, PortDescriptor, Sample, StateError, StateMap, Tick, TypedSample, Value, ValueKind,
};
use std::collections::BTreeMap;

/// The error type a component's [`step`](Component::step) reports.
///
/// Components are free to fail with any error; the executor records the
/// failure against the component and continues the scan.
pub type StepError = Box<dyn std::error::Error>;

/// One logical I/O point a [`Component`] requires.
///
/// The executor checks every requirement against the driver's point map at
/// wiring time — the point must be served, and `direction` and `kind` must
/// match the map exactly — and a step may only touch declared points, in
/// their declared direction.
#[derive(Debug, Clone, PartialEq)]
pub struct IoRequirement {
    /// The requirement's name within the component, used in diagnostics.
    pub name: String,
    /// The logical point the component binds to.
    pub point: PointId,
    /// How the component uses the point: `In` to read it, `Out` to write it.
    pub direction: Direction,
    /// The point's declared value kind. Access is strict: a declared `f64`
    /// point is never coerced to or from another [`Value`](dcs_core::Value)
    /// variant.
    pub kind: ValueKind,
}

impl IoRequirement {
    /// Declares a typed input: [`Direction::In`] with `T`'s value kind.
    pub fn input<T: PointType>(name: impl Into<String>, point: PointId) -> Self {
        Self {
            name: name.into(),
            point,
            direction: Direction::In,
            kind: T::KIND,
        }
    }

    /// Declares a typed output: [`Direction::Out`] with `T`'s value kind.
    pub fn output<T: PointType>(name: impl Into<String>, point: PointId) -> Self {
        Self {
            name: name.into(),
            point,
            direction: Direction::Out,
            kind: T::KIND,
        }
    }
}

/// The I/O surface an [`Executor`](crate::Executor) hands a component during
/// [`Component::step`].
///
/// A `ComponentIo` is a scoped [`IoDriver`]: it serves exactly the points
/// the component declared, reading `In` points from the scan's input image
/// and writing `Out` points into the output image the executor flushes to
/// the field driver after the step phase. Accessing an undeclared point —
/// or a declared point in the wrong direction — fails with
/// [`IoError::UnknownPoint`], and writing the wrong
/// [`Value`](dcs_core::Value) variant fails with
/// [`IoError::TypeMismatch`]; accessors never coerce.
///
/// Typed access goes through [`ComponentIoExt`] (`read_typed` /
/// `write_typed`), blanket-implemented for every `ComponentIo`; being an
/// [`IoDriver`], the view also plugs into the typed
/// [`Input`](dcs_core::Input)/[`Output`](dcs_core::Output) handles.
pub trait ComponentIo: IoDriver {
    /// Writes a full sample — value plus quality — to a declared `Out`
    /// point.
    ///
    /// Components use this to propagate degraded input quality onto their
    /// outputs: [`IoDriver::write`] always stores
    /// [`Quality::Good`](dcs_core::Quality::Good), while `write_sample`
    /// keeps `sample`'s quality. The executor stamps the scan tick over
    /// `sample.tick`; `sample.value`'s kind must match the declared kind.
    fn write_sample(&self, point: PointId, sample: Sample) -> Result<(), IoError>;
}

/// Typed convenience methods on [`ComponentIo`].
///
/// The methods are generic, so they live on this blanket-implemented
/// extension trait — `ComponentIo` itself stays object-safe for
/// `&dyn ComponentIo`. Importing `ComponentIoExt` makes them callable on
/// the `io` view `step` receives.
pub trait ComponentIoExt: ComponentIo {
    /// Reads `point` and decodes the stored sample as `T`.
    ///
    /// Strict, never coercing — a stored value of any other
    /// [`Value`](dcs_core::Value) variant is [`IoError::TypeMismatch`].
    /// Quality and tick pass through so components keep defining behavior
    /// for non-good inputs.
    fn read_typed<T: PointType>(&self, point: PointId) -> Result<TypedSample<T>, IoError> {
        let sample = self.read(point)?;
        let value = T::from_value_exact(sample.value).ok_or(IoError::TypeMismatch {
            point,
            expected: T::KIND,
            found: sample.value,
        })?;
        Ok(TypedSample {
            value,
            quality: sample.quality,
            tick: sample.tick,
        })
    }

    /// Writes `value` to `point` with [`Quality::Good`](dcs_core::Quality::Good).
    ///
    /// To carry a degraded input quality onto an output instead, use
    /// [`ComponentIo::write_sample`].
    fn write_typed<T: PointType>(&self, point: PointId, value: T) -> Result<(), IoError> {
        self.write(point, value.into_value())
    }
}

impl<T: ComponentIo + ?Sized> ComponentIoExt for T {}

/// A unit of control logic wired into the cyclic executor.
///
/// Implementors declare their logical I/O up front and never name a
/// device, fieldbus, or address: the executor checks the declarations
/// against the driver's point map at wiring time and hands each `step` a
/// [`ComponentIo`] view serving exactly those points. `step` must be a
/// pure function of its state, its I/O, and `tick` — reading a wall clock
/// or random source would break the run's determinism.
///
/// Components must be [`Send`]: the executor may live on another thread —
/// e.g. shared behind the monitoring server's mutex — and carries its
/// components with it.
pub trait Component: Send {
    /// The component's stable name, used in wiring errors and diagnostics.
    fn name(&self) -> &str;

    /// The logical I/O the component requires.
    ///
    /// Called once at wiring time; the returned set is the only I/O `step`
    /// may touch.
    fn io_requirements(&self) -> Vec<IoRequirement>;

    /// Executes one scan step at `tick`.
    ///
    /// `io` serves the component's declared points: reads observe the
    /// scan's input image and writes land in the output image the executor
    /// flushes to the driver after every component has stepped. A returned
    /// error is recorded against the component and the scan continues —
    /// the component's outputs keep their last written values.
    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError>;

    /// The component's self-description for the monitoring UI.
    ///
    /// The executor reports one [`ComponentDescriptor`] per registered
    /// component in the [`TelemetrySnapshot`](dcs_core::TelemetrySnapshot):
    /// the metadata a faceplate is rendered from. The default derives a
    /// correct descriptor from what the contract already knows — `name`
    /// and `label` take the component's [`name`](Component::name), `kind`
    /// the implementor's Rust type name, every declared
    /// [`IoRequirement`] becomes a [`PortDescriptor`] with no role hint,
    /// and no parameters are declared — so existing components need no
    /// changes. Components override this to report their model `kind`,
    /// [`PortRole`](dcs_core::PortRole) hints, and tunable-parameter
    /// metadata. A [`PortDescriptor::point`] is always reported `None`
    /// here — the bound point is the serving layer's annotation, joined
    /// in when the descriptor is served in a snapshot.
    fn describe(&self) -> ComponentDescriptor {
        ComponentDescriptor {
            name: self.name().to_string(),
            kind: std::any::type_name::<Self>().to_string(),
            label: self.name().to_string(),
            ports: self
                .io_requirements()
                .into_iter()
                .map(|requirement| PortDescriptor {
                    name: requirement.name,
                    direction: requirement.direction,
                    kind: requirement.kind,
                    role: None,
                    point: None,
                })
                .collect(),
            parameters: Vec::new(),
            commands: Vec::new(),
            events: Vec::new(),
        }
    }

    /// Applies an operator's parameter tuning — the component half of
    /// [`Command::SetParameter`](dcs_core::Command).
    ///
    /// The executor calls this at the scan boundary, in deterministic
    /// submission order, after validating the command against
    /// [`describe`](Component::describe): `parameter` is a declared
    /// parameter name, `value`'s kind matches its declared
    /// [`ValueKind`], and a declared
    /// [`ParameterRange`](dcs_core::ParameterRange) is already enforced.
    /// The implementation still owns the kind's own invariants — limits
    /// that must stay ordered, counts that must stay non-negative — and
    /// refuses a value that would break them with a
    /// [`CommandError`] naming this component; a refused parameter
    /// changes nothing.
    ///
    /// The default returns [`CommandError::UnsupportedParameter`]: kinds
    /// opt into tuning by overriding the hook, and only for the
    /// parameters their descriptor declares — an undeclared name must be
    /// rejected, never silently accepted or ignored.
    ///
    /// **Checkpoint obligation:** a tuned parameter is run state. An
    /// implementation that accepts tuning must fold every writable
    /// parameter into its [`capture_state`](Component::capture_state)
    /// vocabulary so [`restore_state`](Component::restore_state) brings
    /// it back — a tracking standby inherits runtime tuning through the
    /// ordinary checkpoint, and a parameter tuned without being captured
    /// diverges on switchover.
    fn apply_parameter(&mut self, parameter: &str, _value: Value) -> Result<(), CommandError> {
        Err(CommandError::UnsupportedParameter {
            component: self.name().to_string(),
            parameter: parameter.to_string(),
        })
    }

    /// Executes a kind-declared named command — the component half of
    /// [`Command::Invoke`](dcs_core::Command::Invoke).
    ///
    /// The executor calls this at the scan boundary, in deterministic
    /// submission order, after validating the invocation against
    /// [`describe`](Component::describe)'s declared
    /// [`CommandDecl`](dcs_core::CommandDecl)s: `command` is a declared
    /// command's `name` and every supplied argument is a declared
    /// [`CommandArgument`](dcs_core::CommandArgument) name whose
    /// [`Value`] variant matches its declared kind. What the schema does
    /// not constrain — a missing optional argument, a value outside the
    /// command's domain, the declared
    /// [`CommandAvailability::KindDeclared`](dcs_core::CommandAvailability)
    /// availability predicate, and every other kind invariant — is the
    /// implementation's to check.
    ///
    /// `Ok` reports the command applied; `Err` refuses it with the
    /// declared refusal reason, which the settled receipt's
    /// [`CommandError::CommandRefused`] carries verbatim — a refused
    /// invocation changes nothing.
    ///
    /// **Checkpoint obligation:** like a tuned parameter, a command's
    /// effect is run state. An implementation that accepts commands must
    /// fold every state the command mutates into its
    /// [`capture_state`](Component::capture_state) vocabulary, so a
    /// tracking standby inherits the effect through the ordinary
    /// checkpoint.
    ///
    /// The default refuses every declared command: kinds opt into the
    /// invoke surface by overriding the hook, and a kind whose
    /// descriptor declares commands it does not serve still settles
    /// every invocation `command_refused` rather than silently
    /// succeeding.
    fn invoke_command(
        &mut self,
        command: &str,
        _arguments: &BTreeMap<String, Value>,
    ) -> Result<(), String> {
        Err(format!(
            "the kind does not serve the declared command {command:?}"
        ))
    }

    /// Drains the events the component emitted — the component half of
    /// the [`EventDecl`](dcs_core::EventDecl) vocabulary
    /// [`describe`](Component::describe) declares.
    ///
    /// The executor calls this after every [`step`](Component::step),
    /// success or failure, so events a failing step produced are not
    /// lost, and forwards the drained set — in the component's own
    /// emission order — to the scan's emitted-event record, stamping
    /// each [`EmittedEvent`]'s `component` with the registered name.
    /// Emissions journal at the producing scan's tick where the
    /// declaration's [`EventRetention`](dcs_core::EventRetention) keeps
    /// them durable.
    ///
    /// The default emits nothing: kinds opt into the declared-event
    /// surface by overriding the hook, and a kind declaring no events
    /// needs no drain.
    fn drain_events(&mut self) -> Vec<EmittedEvent> {
        Vec::new()
    }

    /// Reports the current values of the component's declared parameters
    /// — the read half of the tuning surface
    /// [`apply_parameter`](Component::apply_parameter) writes.
    ///
    /// The executor serves the report in the
    /// [`TelemetrySnapshot`](dcs_core::TelemetrySnapshot)'s per-component
    /// parameter section, filtered to the names
    /// [`describe`](Component::describe) declares, so the descriptor
    /// stays the surface's sole authority: a name the descriptor does
    /// not declare never reaches the snapshot.
    ///
    /// Report from the same fields
    /// [`capture_state`](Component::capture_state) checkpoints — one
    /// vocabulary feeds the tracking standby and the faceplate, so the
    /// value the operator sees is the value a standby inherited. The
    /// default reports nothing; kinds declaring no tunable parameters
    /// are unaffected.
    fn report_parameters(&self) -> StateMap {
        StateMap::new()
    }

    /// Captures the component's internal state into a [`StateMap`].
    ///
    /// This is the component half of the state-capture contract behind
    /// [`Executor::checkpoint`](crate::Executor::checkpoint): every value
    /// `step` carries between scans — integrators, previous inputs, held
    /// outputs — belongs in the map so a restored component continues the
    /// run exactly as the captured one would have. The default captures
    /// nothing; stateless components are unaffected by checkpointing.
    /// Components override this and
    /// [`restore_state`](Component::restore_state) as a pair.
    fn capture_state(&self) -> StateMap {
        StateMap::new()
    }

    /// Restores state produced by an equivalent component's
    /// [`capture_state`](Component::capture_state).
    ///
    /// Implementations must validate the whole map before applying it —
    /// a rejected restore changes nothing — and fail with a
    /// [`StateError`] naming the component and the offending field when
    /// the map is incompatible: missing fields, wrong value kinds, or
    /// fields the component never captured. The default, for components
    /// that capture nothing, accepts only an empty map.
    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_empty(self.name())
    }
}
