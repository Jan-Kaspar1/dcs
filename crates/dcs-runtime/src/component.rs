//! The component contract: declared logical I/O plus a deterministic step.
//!
//! [`Component`] is what reusable control logic implements and what the
//! [`Executor`](crate::Executor) drives. Declarations are data —
//! [`IoRequirement`] values — so the executor can verify them against the
//! driver's point map before anything runs and scope each step's I/O to
//! exactly the declared set.

use dcs_core::{
    Direction, IoDriver, IoError, PointId, PointType, Sample, Tick, TypedSample, ValueKind,
};

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
pub trait Component {
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
}
