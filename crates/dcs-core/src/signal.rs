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
///
/// Variants serialize in `snake_case` (`{"bool": …}`, `{"int": …}`,
/// `{"float": …}`); the legacy PascalCase spellings remain accepted on read
/// through the variant aliases.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Value {
    /// A boolean point, e.g. a digital input, command, or state.
    #[serde(alias = "Bool")]
    Bool(bool),
    /// A 64-bit signed integer, e.g. a counter or enumerated state.
    #[serde(alias = "Int")]
    Int(i64),
    /// A 64-bit IEEE-754 float, e.g. an analog measurement.
    #[serde(alias = "Float")]
    Float(f64),
}

impl Value {
    /// The kind of this value's variant.
    pub fn kind(self) -> ValueKind {
        match self {
            Value::Bool(_) => ValueKind::Bool,
            Value::Int(_) => ValueKind::Int,
            Value::Float(_) => ValueKind::Float,
        }
    }
}

/// The kind of a [`Value`] variant. Used wherever a value type must be named
/// without a concrete value: coercion targets, declared I/O point types, and
/// type-mismatch reporting.
///
/// Variants serialize in `snake_case` (`"bool"`, `"int"`, `"float"`); the
/// legacy PascalCase spellings remain accepted on read.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ValueKind {
    /// `bool`
    #[serde(alias = "Bool")]
    Bool,
    /// `i64`
    #[serde(alias = "Int")]
    Int,
    /// `f64`
    #[serde(alias = "Float")]
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
    /// The kind of value the coercion was asked to produce.
    pub target: ValueKind,
}

impl CoercionError {
    fn new(value: Value, target: ValueKind) -> Self {
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
            _ => Err(CoercionError::new(value, ValueKind::Bool)),
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
        result.ok_or_else(|| CoercionError::new(value, ValueKind::Int))
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
        result.ok_or_else(|| CoercionError::new(value, ValueKind::Float))
    }
}

/// A machine-readable reason a [`Quality`] is not [`Quality::Good`].
///
/// Ordering follows declaration order and is only used to make
/// [`Quality::merge`] deterministic when both inputs share a severity; it
/// carries no semantic meaning on its own.
///
/// Variants serialize in `snake_case` (`"unspecified"`, `"substituted"`, …);
/// the legacy PascalCase spellings remain accepted on read.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum QualityReason {
    /// No specific reason was reported.
    #[serde(alias = "Unspecified")]
    Unspecified,
    /// The value was substituted: forced, manually entered, or simulated
    /// rather than read from the field.
    #[serde(alias = "Substituted")]
    Substituted,
    /// The value is older than the expected update rate.
    #[serde(alias = "Stale")]
    Stale,
    /// The raw value fell outside the valid operating range.
    #[serde(alias = "OutOfRange")]
    OutOfRange,
    /// Communication with the field device failed or timed out.
    #[serde(alias = "CommunicationFault")]
    CommunicationFault,
    /// The field device or sensor reported a fault.
    #[serde(alias = "DeviceFault")]
    DeviceFault,
    /// The point is not mapped or is misconfigured.
    #[serde(alias = "ConfigurationFault")]
    ConfigurationFault,
}

/// How much a [`Sample`]'s value can be trusted.
///
/// Variant order defines severity (`Good` < `Uncertain` < `Bad`), so the
/// derived `Ord` ranks worse qualities higher and [`Quality::merge`] is just
/// `Ord::max`.
///
/// Variants serialize in `snake_case` (`"good"`, `{"uncertain": …}`,
/// `{"bad": …}`); the legacy PascalCase spellings remain accepted on read.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Quality {
    /// The value is valid and up to date.
    #[serde(alias = "Good")]
    Good,
    /// The value is usable but degraded, e.g. substituted or stale.
    #[serde(alias = "Uncertain")]
    Uncertain(QualityReason),
    /// The value must not be used for control, e.g. a device or
    /// communication fault.
    #[serde(alias = "Bad")]
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

/// A logical timestamp: a deterministic tick count, not wall-clock time.
///
/// The one type serves three distinct tick domains — the executor's *run
/// tick* (its scan counter and the run's journal, history, and receipt
/// attribution domain), the simulated field's *plant tick* (its step
/// counter), and a tracked checkpoint stream's *source tick* — and the
/// value itself does not record which domain minted it. Ticks are
/// comparable only within the domain that produced them; cross-domain
/// ordering or subtraction is meaningful only after explicit translation,
/// like a tracking apply's `tick + tick_offset`. The repository's
/// `CONTEXT.md` names the domains.
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
    /// The logical tick at which the value was sampled, in the tick
    /// domain of whoever stamped it — a run tick on the scan-image
    /// samples the executor stamps, the driver's own domain (a sim
    /// driver's plant tick) on driver-returned samples.
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
    fn value_emits_snake_case_and_reads_legacy_pascal_case() {
        // The emitted wire spelling of every variant.
        assert_eq!(
            serde_json::to_string(&Value::Bool(true)).unwrap(),
            r#"{"bool":true}"#
        );
        assert_eq!(
            serde_json::to_string(&Value::Int(-3)).unwrap(),
            r#"{"int":-3}"#
        );
        assert_eq!(
            serde_json::to_string(&Value::Float(2.5)).unwrap(),
            r#"{"float":2.5}"#
        );
        assert_eq!(
            serde_json::to_string(&ValueKind::Bool).unwrap(),
            r#""bool""#
        );
        assert_eq!(serde_json::to_string(&ValueKind::Int).unwrap(), r#""int""#);
        assert_eq!(
            serde_json::to_string(&ValueKind::Float).unwrap(),
            r#""float""#
        );

        // Persisted artifacts written in the legacy PascalCase spellings
        // still deserialize through the variant aliases.
        assert_eq!(
            serde_json::from_str::<Value>(r#"{"Bool":true}"#).unwrap(),
            Value::Bool(true)
        );
        assert_eq!(
            serde_json::from_str::<Value>(r#"{"Int":-3}"#).unwrap(),
            Value::Int(-3)
        );
        assert_eq!(
            serde_json::from_str::<Value>(r#"{"Float":2.5}"#).unwrap(),
            Value::Float(2.5)
        );
        assert_eq!(
            serde_json::from_str::<ValueKind>(r#""Bool""#).unwrap(),
            ValueKind::Bool
        );
        assert_eq!(
            serde_json::from_str::<ValueKind>(r#""Int""#).unwrap(),
            ValueKind::Int
        );
        assert_eq!(
            serde_json::from_str::<ValueKind>(r#""Float""#).unwrap(),
            ValueKind::Float
        );
    }

    #[test]
    fn quality_emits_snake_case_and_reads_legacy_pascal_case() {
        // The emitted wire spelling: good is a bare string; the degraded
        // severities carry their reason — itself snake_case.
        assert_eq!(serde_json::to_string(&Quality::Good).unwrap(), r#""good""#);
        assert_eq!(
            serde_json::to_string(&Quality::Uncertain(QualityReason::Stale)).unwrap(),
            r#"{"uncertain":"stale"}"#
        );
        assert_eq!(
            serde_json::to_string(&Quality::Bad(QualityReason::DeviceFault)).unwrap(),
            r#"{"bad":"device_fault"}"#
        );
        for (reason, emitted, legacy) in [
            (QualityReason::Unspecified, "unspecified", "Unspecified"),
            (QualityReason::Substituted, "substituted", "Substituted"),
            (QualityReason::Stale, "stale", "Stale"),
            (QualityReason::OutOfRange, "out_of_range", "OutOfRange"),
            (
                QualityReason::CommunicationFault,
                "communication_fault",
                "CommunicationFault",
            ),
            (QualityReason::DeviceFault, "device_fault", "DeviceFault"),
            (
                QualityReason::ConfigurationFault,
                "configuration_fault",
                "ConfigurationFault",
            ),
        ] {
            assert_eq!(
                serde_json::to_string(&reason).unwrap(),
                format!("\"{emitted}\"")
            );
            assert_eq!(
                serde_json::from_str::<QualityReason>(&format!("\"{legacy}\"")).unwrap(),
                reason
            );
        }

        // Legacy spellings on both levels of a quality object.
        assert_eq!(
            serde_json::from_str::<Quality>(r#""Good""#).unwrap(),
            Quality::Good
        );
        assert_eq!(
            serde_json::from_str::<Quality>(r#"{"Uncertain":"Substituted"}"#).unwrap(),
            Quality::Uncertain(QualityReason::Substituted)
        );
        assert_eq!(
            serde_json::from_str::<Quality>(r#"{"Bad":"CommunicationFault"}"#).unwrap(),
            Quality::Bad(QualityReason::CommunicationFault)
        );
        // … and a legacy severity can wrap a canonical reason.
        assert_eq!(
            serde_json::from_str::<Quality>(r#"{"Uncertain":"stale"}"#).unwrap(),
            Quality::Uncertain(QualityReason::Stale)
        );
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
            target: ValueKind::Int,
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
            Err(CoercionError::new(Value::Int(2), ValueKind::Bool))
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
