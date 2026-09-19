//! Reusable control components for the cyclic executor.
//!
//! Each component implements [`dcs_runtime::Component`]: it declares its
//! logical I/O up front — names, point ids, value kinds, directions — and
//! steps once per scan through the scoped [`ComponentIo`] view, never
//! naming a device or fieldbus. Binding the declared points to physical
//! channels is the plant model's and the driver's business, so every
//! component here runs unchanged against `dcs-sim` or field hardware.
//!
//! Components are constructible from the plant model's component
//! [`Parameters`] maps via `from_parameters`; a [`ParameterError`] names
//! the component and the offending parameter when the map cannot supply
//! the documented settings.
//!
//! The first library covers the basic analog and discrete channel blocks:
//!
//! - [`AnalogInput`] — raw-range to engineering-unit scaling with
//!   clamping and quality propagation;
//! - [`AnalogOutput`] — engineering-unit to raw-range scaling, the
//!   inverse of [`AnalogInput`];
//! - [`DigitalInput`] — boolean read with optional inversion and
//!   tick-based debounce;
//! - [`DigitalOutput`] — boolean write with quality propagation;
//! - [`Pid`] — parallel-form PID control with output limits and
//!   conditional-integration anti-windup;
//! - [`AlarmMonitor`] — high/low limit checking with hysteresis on an
//!   analog signal;
//! - [`LatchingAlarm`] — the same limit checking plus an
//!   operator-acknowledgment latch driven by an `ack` input;
//! - [`BoolLatchingAlarm`] — the Bool-input latching sibling: `alarm`
//!   follows a Bool condition and `unacknowledged` latches its fresh
//!   assertion until the same `ack` rule clears it;
//! - [`ManagedLatchingAlarm`] and [`ManagedBoolLatchingAlarm`] — the
//!   managed siblings: each latching kind's vocabulary plus the
//!   shelving, suppression, and out-of-service lifecycle decisions
//!   71–73 record;
//! - [`Interlock`] — analog pass-through gated by Bool trip inputs and a
//!   permissive, driving a configured safe value while tripped;
//! - [`OverrideSelect`] — deterministic selection between a control and
//!   an operator value with worst-of quality propagation;
//! - [`Valve`] — analog actuator with a position-feedback discrepancy
//!   diagnostic;
//! - [`Motor`] — discrete actuator with a run-feedback fault diagnostic
//!   covering failure to start and failure to stop;
//! - [`Timer`] — on-delay/off-delay timing of a Boolean signal in ticks;
//! - [`Counter`] — rising-edge counting with a preset-reached flag and a
//!   reset input;
//! - [`RateLimiter`] — an analog output slewing toward its input by a
//!   bounded per-tick delta;
//! - [`ManualStation`] — a manual/auto station slewing bumplessly to the
//!   newly selected source by a configured per-tick delta;
//! - [`SignalFilter`] — a first-order per-tick smoothing of an analog
//!   signal;
//! - [`MedianVoter`] — 2oo3 median voting over three redundant analog
//!   inputs with a spread discrepancy diagnostic;
//! - [`Totalizer`] — a rate input accumulated into a running total with
//!   reset and rollover;
//! - [`Sequencer`] — a declared table of steps walked in order while a
//!   `run` input holds, each driving `out` for its tick duration;
//! - [`BoolGate`] — an N-input `and`/`or`/`xor` truth fold over its
//!   `in_N` inputs;
//! - [`PumpGroup`] — an N-pump duty/standby group with a declared
//!   rotation policy, lag staging on unmet demand, availability
//!   exclusion, and automatic duty handover on failed feedback;
//! - [`SrLatch`] — a set/reset latch, reset-dominant on simultaneous
//!   assertion;
//! - [`EdgeTrigger`] — a one-scan pulse on the rising, falling, or both
//!   edges of a Boolean input;
//! - [`ThresholdChain`] — the station's ordered start/stop setpoint
//!   table driving a pump-stage demand, with hysteresis and a declared
//!   bad-measurement fallback;
//! - [`FailoverSelect`] — quality-driven selection between a primary
//!   and a backup analog measurement, alarming the backup-mode
//!   transition;
//! - [`FlowPacedRatio`] — the chemical-dosing `dose × flow` demand
//!   with an optional analyzer `trim`, declared dose and rate bounds,
//!   and declared responses to untrusted inputs;
//! - [`DeviationMonitor`] — the dose-confirmation check: the commanded
//!   and measured chemical rates or totals accumulated over a declared
//!   window, the relative deviation tripping `deviating` past the
//!   declared limit;
//! - [`PhaseMonitor`] — the phase-conditioned verification checks
//!   decision 61 records: a declared bound on `in` or on
//!   `in − baseline` evaluated while a condition window stands, a
//!   captured reference, and a deadline flagging a bound never met;
//! - [`BackwashCoordinator`] — shared-supply backwash arbitration: an
//!   ordered request queue and an exclusive held grant gated by the
//!   declared permissives, with a declared queue policy and operator
//!   reorder;
//! - [`BackwashSequence`] — the per-filter backwash step contract:
//!   timed and measured step advance with declared overrun policy, the
//!   coordinator grant handshake, attributed triggers with an
//!   auto-start permission, and the declared abort/fault-step paths.
//! - [`HeaderCoordinator`] — shared aeration-header coordination: the
//!   declared constant-pressure, most-open-valve-reset, or
//!   direct-airflow strategy driving the bounded set-point and
//!   floored demand, plus the capped pulse-grant set.
//! - [`BlowerGroup`] — an N-blower staged group on a continuous
//!   capacity demand: the declared equal split clamped to per-unit
//!   bounds, the vent-based join/departure choreography behind
//!   min-run and start-interval protection, the declared rotation and
//!   staging-authority policies, and the `transition` freeze surface.
//! - [`SurgeGuard`] — the machine-protection demand bound decision 64
//!   records: a blower's capacity demand bounded against the declared
//!   flow-versus-pressure (and optional minimum-current) surge region,
//!   clamping or tripping per the declared response and honoring the
//!   hardwired proven `surge_trip` unconditionally.
//! - [`DemandFallback`] — the declared all-measurements-bad demand
//!   response decision 65 records: while the selected `pv` reads
//!   `Good` the demand passes; a non-`Good` `pv` engages the declared
//!   hold/fixed/safe response with `fallback_active` asserted.
//! - [`FeedforwardSum`] — the additive trim decision 66 records: the
//!   bounded `ff` + `trim` sum on a zone's demand path, the trim's
//!   authority and the emitted demand bounded, each input's declared
//!   untrusted response engaged with `fallback_active` asserted.
//! - [`RateOfRise`] — the one-sided derivative annunciation: the
//!   per-scan first difference of `in` on `rate`, `rising` asserting
//!   while the per-tick rise meets the declared bound — the kind the
//!   IJmuiden composition's recorded divergence-detector gap revisits.
//!
//! Components usable with the model-driven registry expose a `KIND`
//! constant naming the component kind the registry maps onto their
//! `from_parameters` constructor, and describe themselves to the
//! monitoring UI through a
//! [`Component::describe`](dcs_runtime::Component::describe) override
//! built with the shared [`describe`] helpers — kind string, role-hinted
//! ports, and parameter metadata declared beside `KIND` and
//! `from_parameters`.

#![warn(missing_docs)]

mod alarm_monitor;
mod analog_input;
mod analog_output;
mod backwash_coordinator;
mod backwash_sequence;
mod blower_group;
mod bool_gate;
mod bool_latching_alarm;
mod counter;
mod demand_fallback;
pub mod describe;
mod deviation_monitor;
mod digital_input;
mod digital_output;
mod edge_trigger;
mod failover_select;
mod feedforward_sum;
mod flow_paced_ratio;
mod header_coordinator;
mod interlock;
mod latching_alarm;
mod managed;
mod managed_bool_latching_alarm;
mod managed_latching_alarm;
mod manual_station;
mod median_voter;
mod motor;
mod override_select;
mod params;
mod phase_monitor;
mod pid;
mod pump_group;
mod rate_limiter;
mod rate_of_rise;
mod rationalization;
mod sequencer;
mod signal_filter;
mod sr_latch;
mod surge_guard;
mod threshold_chain;
mod timer;
mod totalizer;
mod valve;

pub use alarm_monitor::{AlarmLimits, AlarmMonitor};
pub use analog_input::{AnalogInput, RawInput, Scaling};
pub use analog_output::{AnalogOutput, RawOutput};
pub use backwash_coordinator::{
    BackwashCoordinator, BackwashCoordinatorConfig, CoordinatorOutputs, FilterIo, PermissiveInputs,
    QueuePolicy, QueuedState,
};
pub use backwash_sequence::{
    AdvanceMode, AutoStart, BackwashSequence, BackwashSequenceConfig, BackwashSequenceInputs,
    BackwashSequenceOutputs, BackwashStep, FaultPolicy, OverrunPolicy,
};
pub use blower_group::{
    BlowerGroup, BlowerGroupConfig, BlowerIo, BlowerOutputs, BlowerRotation, StagingAuthority,
    UnitBounds,
};
pub use bool_gate::{BoolGate, GateOperation};
pub use bool_latching_alarm::BoolLatchingAlarm;
pub use counter::Counter;
pub use demand_fallback::{
    DemandFallback, DemandFallbackConfig, DemandFallbackIo, FallbackResponse,
};
pub use deviation_monitor::DeviationMonitor;
pub use digital_input::DigitalInput;
pub use digital_output::DigitalOutput;
pub use edge_trigger::{Edge, EdgeTrigger};
pub use failover_select::FailoverSelect;
pub use feedforward_sum::{
    BadTermResponse, FeedforwardSum, FeedforwardSumConfig, FeedforwardSumIo,
};
pub use flow_paced_ratio::{FlowPacedRatio, FlowPacedRatioConfig, RatioOutputs};
pub use header_coordinator::{
    CoordinationStrategy, HeaderCoordinator, HeaderCoordinatorConfig, HeaderOutputs, ZoneIo,
};
pub use interlock::Interlock;
pub use latching_alarm::LatchingAlarm;
pub use managed::{ManagedAlarmConfig, ManagedAlarmIo};
pub use managed_bool_latching_alarm::ManagedBoolLatchingAlarm;
pub use managed_latching_alarm::ManagedLatchingAlarm;
pub use manual_station::ManualStation;
pub use median_voter::MedianVoter;
pub use motor::Motor;
pub use override_select::OverrideSelect;
pub use params::{ParameterError, Parameters};
pub use phase_monitor::{PhaseMode, PhaseMonitor, PhaseMonitorIo};
pub use pid::{Pid, PidConfig};
pub use pump_group::{GroupOutputs, PumpGroup, PumpGroupConfig, PumpIo, RotationPolicy};
pub use rate_limiter::RateLimiter;
pub use rate_of_rise::RateOfRise;
pub use rationalization::Rationalization;
pub use sequencer::{Sequencer, SequencerStep};
pub use signal_filter::SignalFilter;
pub use sr_latch::SrLatch;
pub use surge_guard::{GuardResponse, SurgeGuard, SurgeGuardConfig, SurgeGuardIo};
pub use threshold_chain::{SetpointTable, ThresholdChain, ThresholdOutputs};
pub use timer::Timer;
pub use totalizer::Totalizer;
pub use valve::Valve;

/// Every component kind the library ships — the set `dcs-controller`'s
/// standard registry registers.
///
/// This is the checked-in kind list the spec-coverage guard is recorded
/// against: `dcs-blocks/tests/spec_drift.rs` asserts the `dcs-build`
/// spec table covers exactly these kinds, and `dcs-controller`'s
/// registry test asserts the standard registry registers exactly these.
/// Adding a kind means adding its `KIND` here, its registry entry, and
/// its `dcs-build` spec — the two tests keep the three in step, so a
/// registered kind without a spec fails.
pub const KINDS: &[&str] = &[
    AnalogInput::<f64>::KIND,
    AnalogOutput::<f64>::KIND,
    Pid::KIND,
    DigitalInput::KIND,
    DigitalOutput::KIND,
    AlarmMonitor::KIND,
    LatchingAlarm::KIND,
    BoolLatchingAlarm::KIND,
    ManagedLatchingAlarm::KIND,
    ManagedBoolLatchingAlarm::KIND,
    Interlock::KIND,
    OverrideSelect::KIND,
    Valve::KIND,
    Motor::KIND,
    Timer::KIND,
    Counter::KIND,
    RateLimiter::KIND,
    ManualStation::KIND,
    SignalFilter::KIND,
    MedianVoter::KIND,
    Totalizer::KIND,
    Sequencer::KIND,
    BoolGate::KIND,
    PumpGroup::KIND,
    SrLatch::KIND,
    EdgeTrigger::KIND,
    ThresholdChain::KIND,
    FailoverSelect::KIND,
    FlowPacedRatio::KIND,
    DeviationMonitor::KIND,
    BackwashCoordinator::KIND,
    BackwashSequence::KIND,
    HeaderCoordinator::KIND,
    BlowerGroup::KIND,
    PhaseMonitor::KIND,
    SurgeGuard::KIND,
    DemandFallback::KIND,
    FeedforwardSum::KIND,
    RateOfRise::KIND,
];

#[cfg(test)]
pub(crate) mod testutil {
    //! A minimal [`ComponentIo`] for stepping components directly: an
    //! in-memory point store scoped to declared points and directions,
    //! mirroring the executor's `ScopedIo` semantics.

    use dcs_core::{Direction, IoDriver, IoError, PointId, Sample, Tick, Value};
    use dcs_runtime::ComponentIo;
    use std::cell::RefCell;
    use std::collections::HashMap;

    /// Scoped I/O over an in-memory point store. Reads serve `In` points,
    /// writes land on `Out` points; undeclared or wrong-direction access
    /// is `UnknownPoint`, and kind mismatches are `TypeMismatch`.
    pub(crate) struct TestIo {
        declared: HashMap<PointId, (Direction, dcs_core::ValueKind)>,
        samples: RefCell<HashMap<PointId, Sample>>,
    }

    impl TestIo {
        /// Declares `points` as `(point, direction, initial sample)`; each
        /// sample's value kind is the point's declared kind.
        pub(crate) fn new(points: &[(PointId, Direction, Sample)]) -> Self {
            Self {
                declared: points
                    .iter()
                    .map(|&(point, direction, sample)| (point, (direction, sample.value.kind())))
                    .collect(),
                samples: RefCell::new(
                    points
                        .iter()
                        .map(|&(point, _, sample)| (point, sample))
                        .collect(),
                ),
            }
        }

        /// Replaces the stored sample of a point — the field side feeding
        /// an `In` point between steps.
        pub(crate) fn feed(&self, point: PointId, sample: Sample) {
            self.samples.borrow_mut().insert(point, sample);
        }

        /// The last sample stored for `point` — what a component wrote to
        /// an `Out` point.
        pub(crate) fn written(&self, point: PointId) -> Option<Sample> {
            self.samples.borrow().get(&point).copied()
        }
    }

    impl IoDriver for TestIo {
        fn read(&self, point: PointId) -> Result<Sample, IoError> {
            match self.declared.get(&point) {
                Some((Direction::In, _)) => Ok(self.samples.borrow()[&point]),
                _ => Err(IoError::UnknownPoint(point)),
            }
        }

        fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
            self.write_sample(point, Sample::good(value, Tick::ZERO))
        }
    }

    impl ComponentIo for TestIo {
        fn write_sample(&self, point: PointId, sample: Sample) -> Result<(), IoError> {
            match self.declared.get(&point) {
                Some((Direction::Out, kind)) => {
                    if sample.value.kind() != *kind {
                        return Err(IoError::TypeMismatch {
                            point,
                            expected: *kind,
                            found: sample.value,
                        });
                    }
                    self.samples.borrow_mut().insert(point, sample);
                    Ok(())
                }
                _ => Err(IoError::UnknownPoint(point)),
            }
        }
    }
}
