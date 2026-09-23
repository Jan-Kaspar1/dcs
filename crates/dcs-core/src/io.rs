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
use crate::state::{StateError, StateMap};
use serde::{Deserialize, Serialize};
use std::fmt;
use std::marker::PhantomData;

/// An I/O failure reported by an [`IoDriver`] or a typed accessor.
///
/// Every variant carries the offending [`PointId`]; [`IoError::point`]
/// retrieves it uniformly.
///
/// Variants serialize in `snake_case` (`{"unknown_point": …}`,
/// `{"type_mismatch": {…}}`, `{"invalid_value": {…}}`, …); the legacy
/// PascalCase spellings remain accepted on read.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IoError {
    /// No channel is mapped to this point, or the driver does not serve it.
    #[serde(alias = "UnknownPoint")]
    UnknownPoint(PointId),
    /// The connection to the field device serving this point is down.
    #[serde(alias = "Disconnected")]
    Disconnected(PointId),
    /// The device did not answer within the driver's timeout.
    #[serde(alias = "Timeout")]
    Timeout(PointId),
    /// The value's kind does not match the declared kind of the point.
    ///
    /// Drivers report this when a write supplies the wrong [`Value`] variant;
    /// [`Input`] reports it when the stored sample is not the handle's
    /// declared type. Mismatches are errors: accessors never silently coerce.
    #[serde(alias = "TypeMismatch")]
    TypeMismatch {
        /// The offending point.
        point: PointId,
        /// The value kind the point or accessor declares.
        expected: ValueKind,
        /// The value actually stored or supplied.
        found: Value,
    },
    /// The field refused the write: another attachment holds the
    /// field's write-ownership claim — the fencing verdict of the
    /// single-writer arbitration a redundant pair relies on when a
    /// promoted standby takes the field. Reads are never fenced.
    #[serde(alias = "Fenced")]
    Fenced(PointId),
    /// The value's kind matched but the field cannot represent it: a
    /// `Float` carrying NaN or an infinity. Non-finite floats have no
    /// JSON spelling — serde emits `null` — so a driver that stored
    /// one would serve samples its own wire contracts cannot carry
    /// back; the refusal keeps the representability invariant at the
    /// storage boundary rather than letting the field go corrupt.
    #[serde(alias = "InvalidValue")]
    InvalidValue {
        /// The offending point.
        point: PointId,
    },
}

impl IoError {
    /// The point the failure is attributed to.
    pub fn point(&self) -> PointId {
        match *self {
            IoError::UnknownPoint(point)
            | IoError::Disconnected(point)
            | IoError::Timeout(point)
            | IoError::Fenced(point) => point,
            IoError::TypeMismatch { point, .. } | IoError::InvalidValue { point } => point,
        }
    }
}

impl fmt::Display for IoError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match *self {
            IoError::UnknownPoint(point) => write!(f, "unknown I/O point {point:?}"),
            IoError::Disconnected(point) => write!(f, "I/O point {point:?} disconnected"),
            IoError::Timeout(point) => write!(f, "I/O point {point:?} timed out"),
            IoError::Fenced(point) => write!(
                f,
                "I/O point {point:?} write fenced: another attachment owns field writes"
            ),
            IoError::TypeMismatch {
                point,
                expected,
                found,
            } => write!(
                f,
                "I/O point {point:?} expects {expected:?}, found {found:?}"
            ),
            IoError::InvalidValue { point } => {
                write!(f, "I/O point {point:?} refused an unrepresentable value")
            }
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

/// The transport-level link state a driver reports through
/// [`IoDriver::diagnostics`].
///
/// This is link health, not point health: the distinction the I/O-health
/// surface exists to make is that a dead transport and a single faulted
/// channel produce the same per-point [`IoError`]s, but only the dead
/// transport reports [`Disconnected`](LinkState::Disconnected) here.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LinkState {
    /// The link to the field device or plant is up.
    Connected,
    /// The link is down or degraded: requests fail at the transport and
    /// surface per point as [`IoError::Disconnected`]/[`IoError::Timeout`].
    Disconnected,
}

/// The transport-level diagnostics a driver volunteers through
/// [`IoDriver::diagnostics`] — its link state and last protocol failure.
///
/// This surface is deliberately distinct from the per-point [`IoError`]s
/// `read`/`write` return: the executor's I/O-health counters attribute
/// point faults while this section names degradation no single point
/// owns — e.g. a plant server that stopped answering.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DriverDiagnostics {
    /// The driver's reported link state.
    pub link: LinkState,
    /// The most recent transport- or protocol-level failure's
    /// description, if the driver has seen one — e.g. the error that
    /// severed the link.
    pub last_error: Option<String>,
    /// The cyclic process-image exchange counters — `Some` only on a
    /// driver implementing the [`CyclicIoDriver`] contract; absent from
    /// payloads serialized before the cyclic contract existed.
    #[serde(default)]
    pub exchange: Option<ExchangeDiagnostics>,
}

/// The cyclic process-image exchange counters a [`CyclicIoDriver`]
/// reports through [`IoDriver::diagnostics`] — the exchange half of the
/// I/O-health surface.
///
/// These are bus-level counters, not per-point ones: one exchange moves
/// the whole image, so its health aggregates over every point the image
/// covers. The per-point consequences of a failed or short exchange —
/// held samples aging to `Stale`, `Disconnected` escalations — still
/// count under the executor's boundary counters when the scan's reads
/// see them.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ExchangeDiagnostics {
    /// Exchanges the driver has attempted — one per `exchange` call.
    pub attempted: u64,
    /// Exchanges that completed: the staged output image published and
    /// the returned input image latched. `attempted - succeeded` counts
    /// the exchanges that did not complete at all.
    pub succeeded: u64,
    /// Completed exchanges whose working counter fell short — the bus
    /// answered but named fewer stations than the image covers. The
    /// driver attributes the shortfall to a station and degrades only
    /// that station's points; the rest of the image latched.
    pub working_counter_mismatches: u64,
    /// The run tick of the most recent completed exchange — the
    /// acquisition stamp the currently latched input samples carry.
    /// `None` before the first completed exchange.
    pub last_exchange_tick: Option<Tick>,
    /// Exchange deadlines the driver reports missed — a completed
    /// exchange whose frame returned past its deadline counts here as
    /// well as under `succeeded`.
    pub missed_deadlines: u64,
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
    /// The returned [`Sample::tick`] is stamped in the driver's own tick
    /// domain — a remote simulated plant's *plant tick*, the scan's *run
    /// tick* for a [`CyclicIoDriver`]'s latched image — which need not be
    /// the caller's: the executor treats it as change evidence and
    /// re-stamps the landed sample with the scan's run tick rather than
    /// comparing the domains directly.
    ///
    /// Returns [`IoError::UnknownPoint`] when the driver serves no such point.
    fn read(&self, point: PointId) -> Result<Sample, IoError>;

    /// Writes `value` to `point`.
    ///
    /// Returns [`IoError::TypeMismatch`] when `value`'s kind differs from the
    /// kind the plant model declared for the point, and may return
    /// [`IoError::InvalidValue`] when the kind matches but the value is not
    /// representable in the field — a non-finite `Float`.
    fn write(&self, point: PointId, value: Value) -> Result<(), IoError>;

    /// Captures the driver's internal state for checkpointing, or `None`
    /// when the driver holds no transferable state.
    ///
    /// This is the driver half of the state-capture contract: a driver
    /// that implements it returns its state as a [`StateMap`] and accepts
    /// such a map back through [`restore_state`](IoDriver::restore_state),
    /// letting a redundant standby reconstruct the driver's view of the
    /// field. The default returns `None` — a stateless driver — and a
    /// checkpoint then carries no driver section. On live hardware the
    /// standby's driver observes the actual process through its real
    /// channels instead of reconstructing state, so `None` is also the
    /// honest answer for drivers whose state is the plant itself.
    fn capture_state(&self) -> Option<StateMap> {
        None
    }

    /// Restores state previously produced by
    /// [`capture_state`](IoDriver::capture_state).
    ///
    /// Restoring a map the driver did not produce fails with a
    /// [`StateError`] naming the driver and the offending field; drivers
    /// should validate the whole map before applying it so a rejected
    /// restore changes nothing. The default accepts only an empty map —
    /// the stateless case.
    fn restore_state(&self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_empty("driver")
    }

    /// Transport-level diagnostics for the telemetry snapshot's I/O
    /// health section, or `None` when the driver has nothing
    /// transport-level to report.
    ///
    /// Optional per driver kind, like
    /// [`capture_state`](IoDriver::capture_state): a driver fronting a
    /// link — a fieldbus coupler, the remote simulated driver — reports
    /// its [`LinkState`] and last protocol failure so a dead link reads
    /// as link degradation, distinct from the per-point faults the
    /// executor counts. A driver with no transport of its own keeps the
    /// default `None`, and a driver wrapping others — a gate, a fan-out
    /// — should forward or aggregate rather than report its own.
    fn diagnostics(&self) -> Option<DriverDiagnostics> {
        None
    }

    /// The driver's cyclic-exchange surface: `Some` marks a driver
    /// implementing the cyclic field-I/O contract —
    /// [`CyclicIoDriver`] — and the executor then calls
    /// [`exchange`](CyclicIoDriver::exchange) once per scan at the read
    /// boundary. `None`, the default, keeps the per-point
    /// `read`/`write` semantics every existing driver kind has; the
    /// executor never calls `exchange` on it.
    ///
    /// A driver wrapping others forwards or aggregates exactly as it
    /// does for [`diagnostics`](IoDriver::diagnostics): a write gate
    /// passes the covered driver's surface through — the exchange is
    /// not a write, so the gate does not quiesce it — and a fan-out
    /// answers `Some` when any backend is cyclic, its own `exchange`
    /// exchanging each cyclic backend's image in turn.
    fn cyclic(&self) -> Option<&(dyn CyclicIoDriver + Sync)> {
        None
    }
}

/// The cyclic field-I/O exchange contract: the opt-in half of
/// [`IoDriver`] for drivers whose transport exchanges a whole process
/// image once per scan — the shape a real fieldbus like EtherCAT has.
///
/// Where a per-point driver answers each `read`/`write` with its own
/// transport operation, a cyclic driver keeps two local images: the
/// *input image* the last completed [`exchange`](Self::exchange)
/// latched and the *output image* `write` calls stage. `read` serves
/// the latched image and `write` stages the pending image — neither
/// may perform a transport operation, and the
/// [`IoError::UnknownPoint`]/[`IoError::TypeMismatch`] semantics are
/// unchanged.
///
/// The executor detects the contract at wiring through
/// [`IoDriver::cyclic`] and calls `exchange` once per scan after queued
/// commands apply and before the per-point input reads. One exchange
/// publishes the output image staged since the previous exchange —
/// the last scan's write phase plus this scan's applied command
/// writes — and latches the returned input image atomically, stamping
/// each latched sample with the exchange's `tick` as its acquisition
/// stamp. A value a component writes in scan `t` publishes in scan
/// `t + 1`'s exchange: the contract's one-scan actuation delay.
///
/// The acquisition stamp is what makes a point's declared
/// `stale_after_ticks` budget meaningful over a fieldbus: the input
/// phase measures the latched sample's stamp against the scan tick, so
/// when exchanges stop landing the held samples age to
/// [`Quality::Uncertain`]`(`[`QualityReason::Stale`]`)` under the
/// declared budget.
///
/// Failure semantics an implementation must hold:
///
/// - a failed `exchange` completes nothing: the input image holds its
///   previous latch and the staged output image is retained for the
///   next exchange. The executor counts the failure once at the
///   boundary and the driver's
///   [`diagnostics`](IoDriver::diagnostics) reports the link
///   [`LinkState::Disconnected`] — one boundary fault, not a fault per
///   covered point;
/// - while consecutive misses stay under the device's declared
///   `exchange_miss_threshold`, `read` keeps serving the held image;
///   at or past the threshold reads escalate to
///   [`IoError::Disconnected`];
/// - an exchange that completes short — a working-counter shortfall
///   naming one station — latches the answering stations' data and
///   degrades only the named station's points, while an
///   unattributable failure degrades the whole bus;
/// - `write` staging is local and does not escalate: staged outputs
///   publish on the next exchange that completes.
///
/// The `Err`'s [`IoError`] names a point the exchange covers — its
/// [`IoError::point`] is diagnostic attribution for the I/O-health
/// record, not a per-point verdict.
///
/// The trait is deliberately open: device integrations implement it
/// from their own crates, like [`IoDriver`] itself.
pub trait CyclicIoDriver: IoDriver {
    /// Runs one process-image exchange for run tick `tick` — the
    /// caller's scan tick — per the contract above.
    fn exchange(&self, tick: Tick) -> Result<(), IoError>;
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
    /// The logical tick at which the value was sampled — same domain as
    /// the [`Sample`] it was decoded from: a run tick on scan-image
    /// samples, the driver's own domain on driver-returned ones.
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
            IoError::Fenced(PointId(5)),
            IoError::InvalidValue { point: PointId(6) },
        ] {
            let json = serde_json::to_string(&error).unwrap();
            assert_eq!(serde_json::from_str::<IoError>(&json).unwrap(), error);
        }
    }

    #[test]
    fn io_error_emits_snake_case_and_reads_legacy_pascal_case() {
        // The emitted wire spelling of every variant — including the
        // snake_case spellings of the nested ValueKind and Value.
        for (error, emitted) in [
            (IoError::UnknownPoint(PointId(7)), r#"{"unknown_point":7}"#),
            (IoError::Disconnected(PointId(8)), r#"{"disconnected":8}"#),
            (IoError::Timeout(PointId(9)), r#"{"timeout":9}"#),
            (
                IoError::TypeMismatch {
                    point: PointId(4),
                    expected: ValueKind::Float,
                    found: Value::Int(1),
                },
                r#"{"type_mismatch":{"point":4,"expected":"float","found":{"int":1}}}"#,
            ),
            (IoError::Fenced(PointId(5)), r#"{"fenced":5}"#),
            (
                IoError::InvalidValue { point: PointId(6) },
                r#"{"invalid_value":{"point":6}}"#,
            ),
        ] {
            assert_eq!(serde_json::to_string(&error).unwrap(), emitted);
        }

        // Persisted artifacts written in the legacy PascalCase spellings
        // still deserialize through the variant aliases — nested
        // ValueKind/Value in the old spelling too.
        for (legacy, error) in [
            (r#"{"UnknownPoint":7}"#, IoError::UnknownPoint(PointId(7))),
            (r#"{"Disconnected":8}"#, IoError::Disconnected(PointId(8))),
            (r#"{"Timeout":9}"#, IoError::Timeout(PointId(9))),
            (
                r#"{"TypeMismatch":{"point":4,"expected":"Float","found":{"Int":1}}}"#,
                IoError::TypeMismatch {
                    point: PointId(4),
                    expected: ValueKind::Float,
                    found: Value::Int(1),
                },
            ),
            (r#"{"Fenced":5}"#, IoError::Fenced(PointId(5))),
            (
                r#"{"InvalidValue":{"point":6}}"#,
                IoError::InvalidValue { point: PointId(6) },
            ),
        ] {
            assert_eq!(serde_json::from_str::<IoError>(legacy).unwrap(), error);
        }
    }

    #[test]
    fn driver_diagnostics_serde_roundtrip() {
        for diagnostics in [
            // A driver reporting only transport health — a non-cyclic
            // driver, or a cyclic one before its first exchange.
            DriverDiagnostics {
                link: LinkState::Connected,
                last_error: None,
                exchange: None,
            },
            // The cyclic exchange section a `CyclicIoDriver` reports.
            DriverDiagnostics {
                link: LinkState::Disconnected,
                last_error: Some("exchange did not complete".to_string()),
                exchange: Some(ExchangeDiagnostics {
                    attempted: 9,
                    succeeded: 6,
                    working_counter_mismatches: 1,
                    last_exchange_tick: Some(Tick(4)),
                    missed_deadlines: 2,
                }),
            },
        ] {
            let json = serde_json::to_string(&diagnostics).unwrap();
            assert_eq!(
                serde_json::from_str::<DriverDiagnostics>(&json).unwrap(),
                diagnostics
            );
        }

        // Payloads serialized before the cyclic contract existed carry
        // no `exchange` field; the defaulted section still reads them.
        let legacy = r#"{"link":"connected","last_error":null}"#;
        assert_eq!(
            serde_json::from_str::<DriverDiagnostics>(legacy).unwrap(),
            DriverDiagnostics {
                link: LinkState::Connected,
                last_error: None,
                exchange: None,
            }
        );
    }
}
