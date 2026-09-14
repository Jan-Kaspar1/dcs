//! The standby's output gate: quiesced writes at the driver boundary.
//!
//! Per the switchover-semantics decision, a tracking standby must not
//! write the field — exactly one peer owns field writes at a time.
//! [`WriteGate`] is the gating [`IoDriver`] wrapper that decision
//! describes: it sits between the executor and the field-facing driver,
//! passes reads and the state-capture contract through, and accepts and
//! drops writes while closed. The executor's output image still forms —
//! snapshots and checkpoints report what the run would write — but
//! nothing reaches the field until [`open`](WriteGate::open) lifts the
//! gate, the promotion path the follow-up switchover ticket builds on.

use dcs_core::{
    DriverDiagnostics, IoDriver, IoError, PointId, Sample, StateError, StateMap, Value,
};
use std::fmt;
use std::sync::atomic::{AtomicBool, Ordering};

/// An [`IoDriver`] wrapper that quiesces writes until opened.
///
/// Place it between a standby's executor and the driver that reaches the
/// field — in the simulated pair, the remote driver attached to the
/// shared plant. A standby-local simulated driver needs no gate: its
/// plant is a private tracking copy, resynchronized by every
/// checkpoint's driver section, and gating it would freeze the locally
/// simulated state the standby keeps tracking with.
///
/// While closed, [`write`](IoDriver::write) is accepted and dropped —
/// quiesced, not faulted — so the standby's scans succeed and its
/// telemetry shows the outputs the run computes. Because the gate sits
/// at the driver boundary it covers every write, including the
/// scan-boundary writes of queued operator commands.
///
/// A fan-out driver that mixes field-reaching and standby-local
/// backends gates selectively instead: [`closed_covering`](Self::closed_covering)
/// quiesces only the writes routed to field-facing points, so the
/// standby's local simulated backends still see their writes and their
/// private plant keeps tracking.
pub struct WriteGate<'d> {
    inner: &'d (dyn IoDriver + Sync),
    open: AtomicBool,
    covered: Option<Box<dyn Fn(PointId) -> bool + Send + Sync + 'd>>,
}

impl<'d> WriteGate<'d> {
    /// A closed gate over `inner`: writes are quiesced until
    /// [`open`](Self::open).
    pub fn closed(inner: &'d (dyn IoDriver + Sync)) -> Self {
        Self {
            inner,
            open: AtomicBool::new(false),
            covered: None,
        }
    }

    /// A closed gate over `inner` covering only the points `covered`
    /// reports: while the gate is closed those writes are quiesced and
    /// every other write passes through. Use it over a fan-out driver
    /// so a tracking standby's writes to the shared field are quiesced
    /// while writes to its local simulated backends still land.
    pub fn closed_covering(
        inner: &'d (dyn IoDriver + Sync),
        covered: impl Fn(PointId) -> bool + Send + Sync + 'd,
    ) -> Self {
        Self {
            inner,
            open: AtomicBool::new(false),
            covered: Some(Box::new(covered)),
        }
    }

    /// Whether a write to `point` is gated while the gate is closed.
    /// A gate without coverage applies to every point.
    fn covers(&self, point: PointId) -> bool {
        self.covered.as_ref().is_none_or(|covered| covered(point))
    }

    /// Lifts the gate: later writes pass through to `inner`. Promotion
    /// opens the gate at a documented scan boundary; that action is the
    /// follow-up switchover ticket's.
    pub fn open(&self) {
        self.open.store(true, Ordering::Relaxed);
    }

    /// Re-closes the gate: later writes are quiesced again — the
    /// demotion half of the single-writer invariant.
    pub fn close(&self) {
        self.open.store(false, Ordering::Relaxed);
    }

    /// Whether writes currently pass through.
    pub fn is_open(&self) -> bool {
        self.open.load(Ordering::Relaxed)
    }
}

impl fmt::Debug for WriteGate<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("WriteGate")
            .field("open", &self.is_open())
            .finish_non_exhaustive()
    }
}

impl IoDriver for WriteGate<'_> {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        self.inner.read(point)
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        if self.is_open() || !self.covers(point) {
            self.inner.write(point, value)
        } else {
            Ok(())
        }
    }

    fn capture_state(&self) -> Option<StateMap> {
        self.inner.capture_state()
    }

    fn restore_state(&self, state: &StateMap) -> Result<(), StateError> {
        self.inner.restore_state(state)
    }

    /// The gated driver's transport diagnostics pass through: a quiesced
    /// standby still wants its field link's health reported.
    fn diagnostics(&self) -> Option<DriverDiagnostics> {
        self.inner.diagnostics()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use dcs_core::Tick;
    use std::collections::HashMap;
    use std::sync::Mutex;

    struct StubDriver {
        points: Mutex<HashMap<PointId, Sample>>,
    }

    impl StubDriver {
        fn new(point: PointId, value: Value) -> Self {
            Self {
                points: Mutex::new(HashMap::from([(point, Sample::good(value, Tick::ZERO))])),
            }
        }
    }

    impl IoDriver for StubDriver {
        fn read(&self, point: PointId) -> Result<Sample, IoError> {
            self.points
                .lock()
                .unwrap()
                .get(&point)
                .copied()
                .ok_or(IoError::UnknownPoint(point))
        }

        fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
            let mut points = self.points.lock().unwrap();
            let stored = points.get_mut(&point).ok_or(IoError::UnknownPoint(point))?;
            *stored = Sample::good(value, stored.tick);
            Ok(())
        }
    }

    #[test]
    fn closed_gate_quiesces_writes_but_reads_pass_through() {
        let point = PointId(1);
        let driver = StubDriver::new(point, Value::Float(0.0));
        let gate = WriteGate::closed(&driver);

        // The quiesced write is accepted — the executor's scan succeeds —
        // but nothing reaches the field.
        gate.write(point, Value::Float(9.0)).unwrap();
        assert_eq!(gate.read(point).unwrap().value, Value::Float(0.0));
        assert!(!gate.is_open());

        gate.open();
        assert!(gate.is_open());
        gate.write(point, Value::Float(9.0)).unwrap();
        assert_eq!(gate.read(point).unwrap().value, Value::Float(9.0));

        gate.close();
        gate.write(point, Value::Float(1.0)).unwrap();
        assert_eq!(gate.read(point).unwrap().value, Value::Float(9.0));
    }

    #[test]
    fn covered_gate_quiesces_covered_points_only() {
        let field = PointId(1);
        let local = PointId(2);
        let driver = StubDriver::new(field, Value::Float(0.0));
        driver
            .points
            .lock()
            .unwrap()
            .insert(local, Sample::good(Value::Float(0.0), Tick::ZERO));
        let gate = WriteGate::closed_covering(&driver, |point| point == field);

        // Closed: the covered field write is dropped; the uncovered
        // local write still reaches the backend — its private simulated
        // plant keeps tracking.
        gate.write(field, Value::Float(9.0)).unwrap();
        gate.write(local, Value::Float(7.0)).unwrap();
        assert_eq!(gate.read(field).unwrap().value, Value::Float(0.0));
        assert_eq!(gate.read(local).unwrap().value, Value::Float(7.0));

        // Open: every write passes.
        gate.open();
        gate.write(field, Value::Float(9.0)).unwrap();
        assert_eq!(gate.read(field).unwrap().value, Value::Float(9.0));
    }
}
