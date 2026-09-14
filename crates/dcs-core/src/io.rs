//! Hardware abstraction contracts for logical I/O.
//!
//! Control components interact with the process through typed logical points —
//! [`Input`] and [`Output`] handles over a shared [`IoDriver`] — rather than
//! through any bus-specific API. A component declares a [`PointId`] and a Rust
//! value type (`bool`, `i64`, or `f64`, via [`PointType`]); it never names a
//! device, fieldbus, or address.
//!
//! Binding a [`PointId`] to a physical channel — which device, which bus,
//! which address — is supplied by the plant model's I/O mapping and resolved
//! by the concrete driver beneath the runtime, not by control logic. Keeping
//! that mapping out of `dcs-core` is what lets the same component run against
//! simulated I/O, EtherCAT, or local hardware unchanged.

use crate::signal::{PointId, Quality, Sample, Tick, Value, ValueKind};
use serde::{Deserialize, Serialize};
use std::fmt;
use std::marker::PhantomData;

/// An I/O failure reported by an [`IoDriver`] or a typed accessor.
///
/// Every variant carries the offending [`PointId`]; [`IoError::point`]
/// retrieves it uniformly.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub enum IoError {
    /// No channel is mapped to this point, or the driver does not serve it.
    UnknownPoint(PointId),
    /// The connection to the field device serving this point is down.
    Disconnected(PointId),
    /// The device did not answer within the driver's timeout.
    Timeout(PointId),
    /// The value's kind does not match the declared kind of the point.
    ///
    /// Drivers report this when a write supplies the wrong [`Value`] variant;
    /// [`Input`] reports it when the stored sample is not the handle's
    /// declared type. Mismatches are errors: accessors never silently coerce.
    TypeMismatch {
        /// The offending point.
        point: PointId,
        /// The value kind the point or accessor declares.
        expected: ValueKind,
        /// The value actually stored or supplied.
        found: Value,
    },
}

impl IoError {
    /// The point the failure is attributed to.
    pub fn point(&self) -> PointId {
        match *self {
            IoError::UnknownPoint(point)
            | IoError::Disconnected(point)
            | IoError::Timeout(point) => point,
            IoError::TypeMismatch { point, .. } => point,
        }
    }
}

impl fmt::Display for IoError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match *self {
            IoError::UnknownPoint(point) => write!(f, "unknown I/O point {point:?}"),
            IoError::Disconnected(point) => write!(f, "I/O point {point:?} disconnected"),
            IoError::Timeout(point) => write!(f, "I/O point {point:?} timed out"),
            IoError::TypeMismatch {
                point,
                expected,
                found,
            } => write!(
                f,
                "I/O point {point:?} expects {expected:?}, found {found:?}"
            ),
        }
    }
}

impl std::error::Error for IoError {}

/// Data-flow direction of a logical I/O point.
///
/// `In` carries a value from the field into the controller — components
/// read such points. `Out` carries one from the controller to the field —
/// components write them. The same vocabulary names direction in component
/// I/O declarations, the driver's point map, and the plant model's I/O
/// mapping.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Direction {
    /// Field-to-controller; the controller reads the point.
    In,
    /// Controller-to-field; the controller writes the point.
    Out,
}

impl fmt::Display for Direction {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Direction::In => "in",
            Direction::Out => "out",
        })
    }
}

/// The driver-facing contract: untyped access to logical I/O points.
///
/// A driver serves the [`PointId`]s the plant model maps onto its physical
/// channels; how a point reaches a device (local I/O, EtherCAT, Ethernet,
/// simulation) is the driver's business and invisible to callers. Control
/// components should prefer the typed [`Input`]/[`Output`] handles over
/// calling this trait directly.
///
/// The trait is object-safe and takes `&self`, so drivers use interior
/// mutability and many handles can share one `&dyn IoDriver` at once.
pub trait IoDriver {
    /// Reads the most recent sample for `point`.
    ///
    /// Returns [`IoError::UnknownPoint`] when the driver serves no such point.
    fn read(&self, point: PointId) -> Result<Sample, IoError>;

    /// Writes `value` to `point`.
    ///
    /// Returns [`IoError::TypeMismatch`] when `value`'s kind differs from the
    /// kind the plant model declared for the point.
    fn write(&self, point: PointId, value: Value) -> Result<(), IoError>;
}

mod sealed {
    /// Seals [`PointType`](super::PointType): the [`Value`](super::Value)
    /// variant set is closed, so the logical I/O types are exactly `bool`,
    /// `i64`, and `f64`.
    pub trait Sealed {}

    impl Sealed for bool {}
    impl Sealed for i64 {}
    impl Sealed for f64 {}
}

/// A Rust type usable as a logical I/O point's declared value type.
///
/// Each implementor maps to exactly one [`Value`] variant, and matching is
/// strict: a declared `f64` point holding `Value::Int(1)` is a type mismatch,
/// not a coercion opportunity. Callers that want conversion use the
/// `TryFrom<Value>` impls explicitly.
pub trait PointType: Copy + sealed::Sealed {
    /// The [`Value`] variant this type occupies.
    const KIND: ValueKind;

    /// Wraps this value in its [`Value`] variant.
    fn into_value(self) -> Value;

    /// Extracts this type when `value` is exactly the matching variant.
    fn from_value_exact(value: Value) -> Option<Self>;
}

impl PointType for bool {
    const KIND: ValueKind = ValueKind::Bool;

    fn into_value(self) -> Value {
        Value::Bool(self)
    }

    fn from_value_exact(value: Value) -> Option<Self> {
        match value {
            Value::Bool(v) => Some(v),
            _ => None,
        }
    }
}

impl PointType for i64 {
    const KIND: ValueKind = ValueKind::Int;

    fn into_value(self) -> Value {
        Value::Int(self)
    }

    fn from_value_exact(value: Value) -> Option<Self> {
        match value {
            Value::Int(v) => Some(v),
            _ => None,
        }
    }
}

impl PointType for f64 {
    const KIND: ValueKind = ValueKind::Float;

    fn into_value(self) -> Value {
        Value::Float(self)
    }

    fn from_value_exact(value: Value) -> Option<Self> {
        match value {
            Value::Float(v) => Some(v),
            _ => None,
        }
    }
}

/// One observation of a typed point: like [`Sample`], but with the value
/// decoded to the handle's declared type.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct TypedSample<T> {
    /// The decoded value.
    pub value: T,
    /// How much the value can be trusted.
    pub quality: Quality,
    /// The logical tick at which the value was sampled.
    pub tick: Tick,
}

/// A typed logical input bound to one [`PointId`] on a shared driver.
///
/// Declaring `Input<f64>` means "this component reads an analog value here";
/// which physical channel backs the point comes from the plant model and the
/// driver, never from the component. `D` defaults to [`dyn IoDriver`] so
/// components stay driver-agnostic; a concrete driver type can be substituted
/// for static dispatch.
pub struct Input<'d, T, D: IoDriver + ?Sized = dyn IoDriver> {
    driver: &'d D,
    point: PointId,
    marker: PhantomData<fn() -> T>,
}

impl<'d, T, D: IoDriver + ?Sized> Input<'d, T, D> {
    /// Binds a logical point on `driver`.
    pub fn new(driver: &'d D, point: PointId) -> Self {
        Self {
            driver,
            point,
            marker: PhantomData,
        }
    }

    /// The point this handle reads.
    pub fn point(&self) -> PointId {
        self.point
    }
}

impl<T: PointType, D: IoDriver + ?Sized> Input<'_, T, D> {
    /// Reads the point and decodes the sample as `T`.
    ///
    /// The stored value must be exactly `T`'s [`Value`] variant; anything else
    /// is [`IoError::TypeMismatch`] — the accessor never coerces. Quality and
    /// tick pass through so components keep defining behavior for non-good
    /// inputs.
    pub fn read(&self) -> Result<TypedSample<T>, IoError> {
        let sample = self.driver.read(self.point)?;
        let value = T::from_value_exact(sample.value).ok_or(IoError::TypeMismatch {
            point: self.point,
            expected: T::KIND,
            found: sample.value,
        })?;
        Ok(TypedSample {
            value,
            quality: sample.quality,
            tick: sample.tick,
        })
    }
}

/// A typed logical output bound to one [`PointId`] on a shared driver.
///
/// The output half of [`Input`]'s contract: `Output<bool>` means "this
/// component drives a discrete point here", with the physical channel supplied
/// by the plant model and the driver.
pub struct Output<'d, T, D: IoDriver + ?Sized = dyn IoDriver> {
    driver: &'d D,
    point: PointId,
    marker: PhantomData<fn() -> T>,
}

impl<'d, T, D: IoDriver + ?Sized> Output<'d, T, D> {
    /// Binds a logical point on `driver`.
    pub fn new(driver: &'d D, point: PointId) -> Self {
        Self {
            driver,
            point,
            marker: PhantomData,
        }
    }

    /// The point this handle writes.
    pub fn point(&self) -> PointId {
        self.point
    }
}

impl<T: PointType, D: IoDriver + ?Sized> Output<'_, T, D> {
    /// Writes `value` to the point.
    ///
    /// The driver rejects the write with [`IoError::TypeMismatch`] when the
    /// point's declared kind differs from `T`.
    pub fn write(&self, value: T) -> Result<(), IoError> {
        self.driver.write(self.point, value.into_value())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::{Cell, RefCell};
    use std::collections::HashMap;

    /// In-memory driver stub. Each point is declared with an initial value
    /// whose variant stands in for the kind the plant model would declare.
    struct StubDriver {
        points: RefCell<HashMap<PointId, Sample>>,
        tick: Cell<u64>,
    }

    impl StubDriver {
        fn new(points: &[(PointId, Value)]) -> Self {
            Self {
                points: RefCell::new(
                    points
                        .iter()
                        .map(|&(point, value)| (point, Sample::good(value, Tick::ZERO)))
                        .collect(),
                ),
                tick: Cell::new(0),
            }
        }
    }

    impl IoDriver for StubDriver {
        fn read(&self, point: PointId) -> Result<Sample, IoError> {
            self.points
                .borrow()
                .get(&point)
                .copied()
                .ok_or(IoError::UnknownPoint(point))
        }

        fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
            let mut points = self.points.borrow_mut();
            let sample = points.get_mut(&point).ok_or(IoError::UnknownPoint(point))?;
            if value.kind() != sample.value.kind() {
                return Err(IoError::TypeMismatch {
                    point,
                    expected: sample.value.kind(),
                    found: value,
                });
            }
            let tick = Tick(self.tick.get());
            self.tick.set(tick.0 + 1);
            *sample = Sample::good(value, tick);
            Ok(())
        }
    }

    /// A consumer written against the trait alone: no concrete driver type is
    /// named, and `?Sized` admits `dyn IoDriver`.
    fn drive<D: IoDriver + ?Sized>(
        driver: &D,
        point: PointId,
        value: Value,
    ) -> Result<Sample, IoError> {
        driver.write(point, value)?;
        driver.read(point)
    }

    #[test]
    fn write_then_read_roundtrips_value() {
        let point = PointId(1);
        let driver = StubDriver::new(&[(point, Value::Float(0.0))]);

        Output::<f64>::new(&driver, point).write(2.5).unwrap();
        let sample = Input::<f64>::new(&driver, point).read().unwrap();

        assert_eq!(sample.value, 2.5);
        assert!(sample.quality.is_good());
        assert_eq!(sample.tick, Tick(0));
    }

    #[test]
    fn wrong_value_variant_is_type_mismatch() {
        let point = PointId(2);
        let driver = StubDriver::new(&[(point, Value::Int(0))]);

        let error = driver.write(point, Value::Bool(true)).unwrap_err();
        assert_eq!(
            error,
            IoError::TypeMismatch {
                point,
                expected: ValueKind::Int,
                found: Value::Bool(true),
            }
        );
        assert_eq!(error.point(), point);

        // A typed handle whose declared type disagrees with the point's
        // declared kind surfaces the same driver-side error.
        let error = Output::<bool>::new(&driver, point).write(true).unwrap_err();
        assert!(matches!(error, IoError::TypeMismatch { .. }));

        // The stored value is untouched.
        assert_eq!(driver.read(point).unwrap().value, Value::Int(0));
    }

    #[test]
    fn unknown_point_errors() {
        let driver = StubDriver::new(&[]);
        let unknown = PointId(99);

        assert_eq!(driver.read(unknown), Err(IoError::UnknownPoint(unknown)));
        assert_eq!(
            Input::<f64>::new(&driver, unknown).read().unwrap_err(),
            IoError::UnknownPoint(unknown)
        );
        assert_eq!(
            Output::<bool>::new(&driver, unknown)
                .write(true)
                .unwrap_err(),
            IoError::UnknownPoint(unknown)
        );
    }

    #[test]
    fn typed_read_mismatch_is_error_not_coercion() {
        let point = PointId(3);
        let driver = StubDriver::new(&[(point, Value::Int(1))]);

        // Int(1) would coerce losslessly to bool or f64, but strict variant
        // matching reports a mismatch instead of silently converting.
        assert_eq!(
            Input::<bool>::new(&driver, point).read().unwrap_err(),
            IoError::TypeMismatch {
                point,
                expected: ValueKind::Bool,
                found: Value::Int(1),
            }
        );
        assert_eq!(
            Input::<f64>::new(&driver, point).read().unwrap_err(),
            IoError::TypeMismatch {
                point,
                expected: ValueKind::Float,
                found: Value::Int(1),
            }
        );
        assert_eq!(Input::<i64>::new(&driver, point).read().unwrap().value, 1);
    }

    #[test]
    fn consumer_compiles_against_trait_alone() {
        let point = PointId(4);
        let driver = StubDriver::new(&[(point, Value::Bool(false))]);

        // Static dispatch on the concrete stub.
        let sample = drive(&driver, point, Value::Bool(true)).unwrap();
        assert_eq!(sample.value, Value::Bool(true));

        // Dynamic dispatch on a trait object.
        let trait_object: &dyn IoDriver = &driver;
        let sample = drive(trait_object, point, Value::Bool(true)).unwrap();
        assert_eq!(sample.value, Value::Bool(true));

        // Typed handles over the default `dyn IoDriver` parameter.
        let input = Input::<bool>::new(trait_object, point);
        assert_eq!(input.point(), point);
        assert!(input.read().unwrap().value);
    }

    #[test]
    fn io_error_serde_roundtrip() {
        for error in [
            IoError::UnknownPoint(PointId(1)),
            IoError::Disconnected(PointId(2)),
            IoError::Timeout(PointId(3)),
            IoError::TypeMismatch {
                point: PointId(4),
                expected: ValueKind::Float,
                found: Value::Int(1),
            },
        ] {
            let json = serde_json::to_string(&error).unwrap();
            assert_eq!(serde_json::from_str::<IoError>(&json).unwrap(), error);
        }
    }
}
