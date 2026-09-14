//! Signal model: typed values, quality, logical timestamps, and identifiers.
//!
//! A signal is a [`Sample`]: a [`Value`] observed at a logical [`Tick`] with a
//! [`Quality`] describing how much that value can be trusted. Logical I/O
//! points are identified by [`SignalId`] and [`PointId`] newtypes so the
//! contract stays typed rather than stringly keyed.

use serde::{Deserialize, Serialize};
use std::fmt;

/// A typed process value.
///
/// The variant set is deliberately small; widening conversions that cannot
/// lose information are available through the `TryFrom<Value>` impls.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub enum Value {
    /// A boolean point, e.g. a digital input, command, or state.
    Bool(bool),
    /// A 64-bit signed integer, e.g. a counter or enumerated state.
    Int(i64),
    /// A 64-bit IEEE-754 float, e.g. an analog measurement.
    Float(f64),
}

/// The type a [`Value`] coercion was asked to produce.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum CoercionTarget {
    /// `bool`
    Bool,
    /// `i64`
    Int,
    /// `f64`
    Float,
}

/// Error returned when a [`Value`] cannot be coerced without losing
/// information. Coercions are strict: `Int` to `Float` requires exact
/// representability and `Float` to `Int` requires a finite integral value in
/// range.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct CoercionError {
    /// The value that could not be coerced.
    pub value: Value,
    /// The type the coercion was asked to produce.
    pub target: CoercionTarget,
}

impl CoercionError {
    fn new(value: Value, target: CoercionTarget) -> Self {
        Self { value, target }
    }
}

impl fmt::Display for CoercionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "cannot losslessly coerce {:?} to {:?}",
            self.value, self.target
        )
    }
}

impl std::error::Error for CoercionError {}

/// `2^63` as an exactly representable float. `i64::MAX as f64` rounds up to
/// this value, so the `i64` upper bound must be treated as exclusive.
const TWO63: f64 = 9_223_372_036_854_775_808.0;

impl TryFrom<Value> for bool {
    type Error = CoercionError;

    fn try_from(value: Value) -> Result<Self, Self::Error> {
        match value {
            Value::Bool(v) => Ok(v),
            Value::Int(0) => Ok(false),
            Value::Int(1) => Ok(true),
            // Reject -0.0: coercing it back would produce +0.0.
            Value::Float(v) if v == 0.0 && v.is_sign_positive() => Ok(false),
            Value::Float(1.0) => Ok(true),
            _ => Err(CoercionError::new(value, CoercionTarget::Bool)),
        }
    }
}

impl TryFrom<Value> for i64 {
    type Error = CoercionError;

    fn try_from(value: Value) -> Result<Self, Self::Error> {
        let result = match value {
            Value::Bool(v) => Some(i64::from(v)),
            Value::Int(v) => Some(v),
            Value::Float(v)
                if v.is_finite() && v.fract() == 0.0 && (-TWO63..TWO63).contains(&v) =>
            {
                Some(v as i64)
            }
            _ => None,
        };
        result.ok_or_else(|| CoercionError::new(value, CoercionTarget::Int))
    }
}

impl TryFrom<Value> for f64 {
    type Error = CoercionError;

    fn try_from(value: Value) -> Result<Self, Self::Error> {
        let result = match value {
            Value::Bool(v) => Some(if v { 1.0 } else { 0.0 }),
            Value::Int(v) => {
                let f = v as f64;
                // Reject the case where rounding produced +2^63, which would
                // saturate back to i64::MAX and hide the lost bit.
                (f < TWO63 && f as i64 == v).then_some(f)
            }
            Value::Float(v) => Some(v),
        };
        result.ok_or_else(|| CoercionError::new(value, CoercionTarget::Float))
    }
}

/// A machine-readable reason a [`Quality`] is not [`Quality::Good`].
///
/// Ordering follows declaration order and is only used to make
/// [`Quality::merge`] deterministic when both inputs share a severity; it
/// carries no semantic meaning on its own.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub enum QualityReason {
    /// No specific reason was reported.
    Unspecified,
    /// The value was substituted: forced, manually entered, or simulated
    /// rather than read from the field.
    Substituted,
    /// The value is older than the expected update rate.
    Stale,
    /// The raw value fell outside the valid operating range.
    OutOfRange,
    /// Communication with the field device failed or timed out.
    CommunicationFault,
    /// The field device or sensor reported a fault.
    DeviceFault,
    /// The point is not mapped or is misconfigured.
    ConfigurationFault,
}

/// How much a [`Sample`]'s value can be trusted.
///
/// Variant order defines severity (`Good` < `Uncertain` < `Bad`), so the
/// derived `Ord` ranks worse qualities higher and [`Quality::merge`] is just
/// `Ord::max`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub enum Quality {
    /// The value is valid and up to date.
    Good,
    /// The value is usable but degraded, e.g. substituted or stale.
    Uncertain(QualityReason),
    /// The value must not be used for control, e.g. a device or
    /// communication fault.
    Bad(QualityReason),
}

impl Quality {
    /// Returns the worse of two qualities, i.e. the quality a value derived
    /// from both inputs should carry.
    ///
    /// When both inputs share a severity, the one with the greater
    /// [`QualityReason`] discriminant wins; severity always dominates.
    pub fn merge(self, other: Self) -> Self {
        self.max(other)
    }

    /// The reason the quality is not [`Quality::Good`], if any.
    pub fn reason(self) -> Option<QualityReason> {
        match self {
            Quality::Good => None,
            Quality::Uncertain(reason) | Quality::Bad(reason) => Some(reason),
        }
    }

    /// Whether this quality is [`Quality::Good`].
    pub fn is_good(self) -> bool {
        matches!(self, Quality::Good)
    }
}

/// A logical timestamp: the executor's deterministic tick count, not
/// wall-clock time. Ticks are only comparable within the run that produced
/// them.
#[derive(
    Debug, Default, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize,
)]
pub struct Tick(pub u64);

impl Tick {
    /// The first tick of a run.
    pub const ZERO: Tick = Tick(0);
}

/// One observation of a signal: a value, its quality, and the tick at which
/// it was sampled.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Sample {
    /// The observed value.
    pub value: Value,
    /// How much the value can be trusted.
    pub quality: Quality,
    /// The logical tick at which the value was sampled.
    pub tick: Tick,
}

impl Sample {
    /// A sample with an explicit quality.
    pub fn new(value: Value, quality: Quality, tick: Tick) -> Self {
        Self {
            value,
            quality,
            tick,
        }
    }

    /// A [`Quality::Good`] sample.
    pub fn good(value: Value, tick: Tick) -> Self {
        Self::new(value, Quality::Good, tick)
    }
}

/// Identifies a signal in the plant model.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct SignalId(pub u64);

/// Identifies a logical I/O point declared by a component.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct PointId(pub u64);

#[cfg(test)]
mod tests {
    use super::*;
    use serde::de::DeserializeOwned;
    use std::fmt::Debug;

    fn roundtrip<T>(value: T)
    where
        T: Serialize + DeserializeOwned + PartialEq + Debug,
    {
        let json = serde_json::to_string(&value).unwrap();
        assert_eq!(serde_json::from_str::<T>(&json).unwrap(), value);
    }

    #[test]
    fn value_serde_roundtrip() {
        for value in [
            Value::Bool(true),
            Value::Bool(false),
            Value::Int(0),
            Value::Int(-42),
            Value::Int(i64::MAX),
            Value::Float(0.0),
            Value::Float(-2.5),
        ] {
            roundtrip(value);
        }
    }

    #[test]
    fn quality_serde_roundtrip() {
        for reason in [
            QualityReason::Unspecified,
            QualityReason::Substituted,
            QualityReason::Stale,
            QualityReason::OutOfRange,
            QualityReason::CommunicationFault,
            QualityReason::DeviceFault,
            QualityReason::ConfigurationFault,
        ] {
            roundtrip(reason);
            roundtrip(Quality::Uncertain(reason));
            roundtrip(Quality::Bad(reason));
        }
        roundtrip(Quality::Good);
    }

    #[test]
    fn sample_tick_and_id_serde_roundtrip() {
        roundtrip(Tick(7));
        roundtrip(Sample::new(
            Value::Float(1.5),
            Quality::Bad(QualityReason::CommunicationFault),
            Tick(3),
        ));
        roundtrip(SignalId(11));
        roundtrip(PointId(22));
        roundtrip(CoercionError {
            value: Value::Float(0.5),
            target: CoercionTarget::Int,
        });
    }

    #[test]
    fn merge_returns_worst_quality() {
        let uncertain = Quality::Uncertain(QualityReason::Stale);
        let bad = Quality::Bad(QualityReason::DeviceFault);
        let cases = [
            (Quality::Good, Quality::Good, Quality::Good),
            (Quality::Good, uncertain, uncertain),
            (uncertain, Quality::Good, uncertain),
            (Quality::Good, bad, bad),
            (bad, Quality::Good, bad),
            (uncertain, bad, bad),
            (bad, uncertain, bad),
            (
                uncertain,
                Quality::Uncertain(QualityReason::Substituted),
                uncertain,
            ),
            (
                bad,
                Quality::Bad(QualityReason::Unspecified),
                Quality::Bad(QualityReason::DeviceFault),
            ),
        ];
        for (a, b, expected) in cases {
            assert_eq!(a.merge(b), expected, "{a:?} merge {b:?}");
        }
    }

    #[test]
    fn accepted_coercions() {
        assert_eq!(bool::try_from(Value::Bool(true)), Ok(true));
        assert_eq!(bool::try_from(Value::Int(0)), Ok(false));
        assert_eq!(bool::try_from(Value::Int(1)), Ok(true));
        assert_eq!(bool::try_from(Value::Float(1.0)), Ok(true));

        assert_eq!(i64::try_from(Value::Bool(true)), Ok(1));
        assert_eq!(i64::try_from(Value::Int(-7)), Ok(-7));
        assert_eq!(i64::try_from(Value::Float(-3.0)), Ok(-3));
        assert_eq!(i64::try_from(Value::Float(-TWO63)), Ok(i64::MIN));

        assert_eq!(f64::try_from(Value::Bool(false)), Ok(0.0));
        assert_eq!(
            f64::try_from(Value::Int(1_i64 << 53)),
            Ok((1_i64 << 53) as f64)
        );
        assert_eq!(f64::try_from(Value::Int(i64::MIN)), Ok(i64::MIN as f64));
        assert_eq!(f64::try_from(Value::Float(-0.5)), Ok(-0.5));
    }

    #[test]
    fn rejected_coercions() {
        assert_eq!(
            bool::try_from(Value::Int(2)),
            Err(CoercionError::new(Value::Int(2), CoercionTarget::Bool))
        );
        assert!(bool::try_from(Value::Float(0.5)).is_err());
        assert!(bool::try_from(Value::Float(-0.0)).is_err());
        assert!(bool::try_from(Value::Float(f64::NAN)).is_err());

        assert!(i64::try_from(Value::Float(0.5)).is_err());
        assert!(i64::try_from(Value::Float(f64::NAN)).is_err());
        assert!(i64::try_from(Value::Float(f64::INFINITY)).is_err());
        assert!(i64::try_from(Value::Float(TWO63)).is_err());

        // Not exactly representable as f64.
        assert!(f64::try_from(Value::Int((1_i64 << 53) + 1)).is_err());
        assert!(f64::try_from(Value::Int(i64::MAX)).is_err());
    }
}
