//! One logical plant run against two registered driver kinds — the
//! interim WW-FND-002 evidence.
//!
//! The requirement asks that the reference application run unchanged
//! against at least two registered driver kinds; the full proof waits
//! for the M9 station, but the seam is exercised here by a checked-in
//! fixture pair sharing one logical plant:
//!
//! - `fixtures/two_kinds_scripted.json` binds every field channel to a
//!   `sim-scripted` device — tick-indexed playback declared in the
//!   model's device parameters, stepped by the scan cycle.
//! - `fixtures/two_kinds_bus.json` binds the same channels to a
//!   `sim-bus` device — a register-mapped fieldbus reached over its own
//!   binary TCP protocol, served by a [`BusServer`] the run owns, with
//!   the test side driving input registers through a second
//!   [`BusDriver`] attachment.
//!
//! The two documents are identical outside `devices[0]` — the same
//! `io_points`, `signals`, `components`, and `connections`, and the same
//! declared channel set; only the kind and its kind-specific parameters
//! differ. The test target asserts that sharing, so the fixture pair is
//! a recorded instance of "one logical fixture plus a per-kind device
//! overlay".
//!
//! The plant is a small water-station slice: `LT-201`'s raw wet-well
//! level feeds an `analog-input` scaling into the `LIC-201` `pid`, whose
//! output positions `LV-201` through a `valve` actuator checking its
//! field-echoed position feedback (a `point → point` field wire — a
//! cross-backend route on both variants), while `P-201`'s operator start
//! request drives a `motor` verifying its `digital-input`-conditioned
//! run feedback. The operator points — the level setpoint and the start
//! request — are writable internal points; `level_raw` is a writable
//! field `In` point so the run can force the measurement.
//!
//! # The driven run
//!
//! [`run_scripted`] and [`run_bus`] run the identical scenario through
//! the shared driven-mode orchestration [`equivalence`](crate::equivalence)
//! carries — the same externally paced machinery
//! `dcs-controller --driven` uses: an unpaced
//! [`Monitor`](dcs_monitor::Monitor) armed with
//! [`Driven`](dcs_monitor::Driven) wiring, advanced one scan per
//! `POST /scan` request through
//! [`MonitorClient`](dcs_monitor::MonitorClient). Each requested scan
//! carries the plant step inside the request's boundary — and, on the
//! bus variant, the field feed for the next scan — so the field side
//! and the scan stay in lockstep and nothing reads a wall clock. This
//! module supplies only the scenario content: the fixture documents,
//! the field program, the operator actions, and the pins below.
//!
//! The single field program [`FIELD_PROGRAM`] is the scenario's input:
//! `(tick, point, value)` entries naming the value a field `In` point
//! presents to the scan running at `tick`. The scripted fixture encodes
//! it as model-declared script entries — a script entry at driver tick
//! `t` applies on the step after scan `t`, so it is first observed by
//! scan `t + 1`; the test pins that encoding to the table. The bus
//! variant's feed writes the same entries to the device's registers
//! through the field-side attachment, in the same boundary position.
//!
//! Between the documented scans the run submits operator commands —
//! the setpoint move, the pump start and stop, a force and release on
//! `level_raw`, and a mid-run `kp` retune — so the recorded journal
//! carries settled receipts and quality transitions, not just first
//! observations.
//!
//! # What equality means here
//!
//! The assertion compares what the control plane observed: per-scan
//! [`TelemetrySnapshot`](dcs_core::TelemetrySnapshot)s — every point's
//! value, quality, and tick, the component diagnostics, descriptors,
//! live parameters, force set, and the executor-collected I/O-health
//! counters — and the transition journal's
//! [`JournalEntry`](dcs_core::JournalEntry) sequence. One snapshot
//! field is legitimately kind-specific and normalized before
//! comparison: `io_health.driver`, the driver's own volunteered
//! transport diagnostics — the `sim-bus` link reports itself connected
//! while the scripted backend has no transport to report. That is the
//! same normalization `dcs-assembly`'s `sim-bus` test records: the
//! executor's view is identical; each transport's health report is its
//! own.

use dcs_assembly::{DriverRegistry, resolve_drivers};
use dcs_core::{Command, IoDriver, PointId, Value, ValueKind};
use dcs_model::PlantModel;
use dcs_sim_bus::{BusDriver, BusServer};

use crate::equivalence::{self, BankDerivation, driven_run, field_bindings, with_served_bank};

pub use crate::equivalence::{EquivalenceError as TwoKindsError, OperatorAction, VariantRun};

/// The checked-in `sim-scripted` variant of the shared logical plant.
pub const SCRIPTED_DOCUMENT: &str = include_str!("../fixtures/two_kinds_scripted.json");

/// The checked-in `sim-bus` variant: the same document with the device's
/// kind and parameters swapped; its `address` is the
/// [`BUS_ADDRESS_PLACEHOLDER`] a run substitutes its server's bound
/// address for.
pub const BUS_DOCUMENT: &str = include_str!("../fixtures/two_kinds_bus.json");

/// The address placeholder [`BUS_DOCUMENT`] carries — the run serves
/// the device's register bank on an ephemeral port and substitutes the
/// bound address, exactly as `dcs-assembly`'s `mixed_bus` fixture does.
pub const BUS_ADDRESS_PLACEHOLDER: &str = "__BUS_ADDR__";

/// Simulated process time each scan advances — the `dt` passed to
/// [`FanoutDriver::step`](dcs_assembly::FanoutDriver::step) and the
/// period the pid's `dt` parameter is tuned for.
pub const SCAN_PERIOD: f64 = 0.1;

/// The run's documented length in scans.
pub const TOTAL_SCANS: u64 = 64;

/// The shared model's point ids, named for the scenario and its tests.
pub mod points {
    use dcs_core::PointId;

    /// `LT-201` raw wet-well level input, mA — the feed's analog
    /// program; writable so the run can force it.
    pub const LEVEL_RAW: PointId = PointId(10);
    /// `P-201` run feedback input — the feed's bool program: follows
    /// the command late, drops mid-run, returns, then follows the stop.
    pub const PUMP_RUN: PointId = PointId(11);
    /// `LV-201` position feedback input — field-wired to follow
    /// `VALVE_CMD`, driven by the cross-backend route, never fed.
    pub const VALVE_FEEDBACK: PointId = PointId(12);
    /// `LV-201` valve command output, mA.
    pub const VALVE_CMD: PointId = PointId(20);
    /// `P-201` starter command output.
    pub const PUMP_CMD: PointId = PointId(21);
    /// `LIC-201` level setpoint, % — writable internal point.
    pub const LEVEL_SETPOINT: PointId = PointId(50);
    /// `P-201` operator start request — writable internal point.
    pub const PUMP_START: PointId = PointId(51);
    /// `P-201` run-feedback fault — the motor's status output.
    pub const PUMP_FAULT: PointId = PointId(70);
    /// `LV-201` feedback discrepancy — the valve's status output.
    pub const VALVE_DISCREPANCY: PointId = PointId(71);
}

/// One entry of the shared field program: the value `point` presents to
/// the scan running at `tick` — the first scan is tick 1 — holding until
/// the point's next entry.
///
/// The scripted fixture encodes each change as a script entry at driver
/// tick `tick - 1`: a step applies entries whose tick it reaches, and
/// the driven cycle steps after each scan, so the value applied at
/// driver tick `tick - 1` is first read by scan `tick`. The bus
/// variant's feed writes the change's register during the scan
/// boundary preceding scan `tick` — inside the driven `after_scan`
/// hook — which is the same position in the cycle.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct FieldChange {
    /// The scan tick that first observes the value.
    pub tick: u64,
    /// The field `In` point.
    pub point: PointId,
    /// The presented value; its kind is the point's declared kind.
    pub value: Value,
}

/// The scenario's field inputs, in scheduled order — the single program
/// both variants present: the scripted device replays it from its
/// declared script, the test feeds it to the bus device's registers.
pub const FIELD_PROGRAM: &[FieldChange] = &[
    // The wet well starts at 8 mA (25%) and climbs on inflow; the pump
    // draws it down from tick 45 while its feedback is lost, and the
    // well refills once the pump stops.
    FieldChange {
        tick: 1,
        point: points::LEVEL_RAW,
        value: Value::Float(8.0),
    },
    FieldChange {
        tick: 12,
        point: points::LEVEL_RAW,
        value: Value::Float(9.5),
    },
    FieldChange {
        tick: 24,
        point: points::LEVEL_RAW,
        value: Value::Float(11.0),
    },
    FieldChange {
        tick: 36,
        point: points::LEVEL_RAW,
        value: Value::Float(12.5),
    },
    FieldChange {
        tick: 45,
        point: points::LEVEL_RAW,
        value: Value::Float(13.5),
    },
    FieldChange {
        tick: 57,
        point: points::LEVEL_RAW,
        value: Value::Float(12.0),
    },
    // The starter's run contact follows the command three scans late,
    // drops mid-run at tick 45 — the motor flags its fault once the
    // disagreement outlives `fault_ticks` — returns at 53, and follows
    // the stop at 58.
    FieldChange {
        tick: 1,
        point: points::PUMP_RUN,
        value: Value::Bool(false),
    },
    FieldChange {
        tick: 9,
        point: points::PUMP_RUN,
        value: Value::Bool(true),
    },
    FieldChange {
        tick: 45,
        point: points::PUMP_RUN,
        value: Value::Bool(false),
    },
    FieldChange {
        tick: 53,
        point: points::PUMP_RUN,
        value: Value::Bool(true),
    },
    FieldChange {
        tick: 58,
        point: points::PUMP_RUN,
        value: Value::Bool(false),
    },
];

/// The scenario's operator actions, in submission order — identical for
/// both variants, so their settled receipts journal identically.
pub fn actions() -> Vec<OperatorAction> {
    vec![
        // The pump start request — applied at scan 6; the feedback
        // follows at 9, inside `fault_ticks`.
        OperatorAction {
            tick: 6,
            command: Command::WriteValue {
                point: points::PUMP_START,
                kind: ValueKind::Bool,
                value: Value::Bool(true),
            },
        },
        // The operator's setpoint move, applied at scan 11.
        OperatorAction {
            tick: 11,
            command: Command::WriteValue {
                point: points::LEVEL_SETPOINT,
                kind: ValueKind::Float,
                value: Value::Float(62.0),
            },
        },
        // A force on the field measurement, applied at scan 31: the
        // image substitutes the pinned value stamped
        // `Uncertain(Substituted)` — a quality transition both kinds
        // journal identically, since the force never touches the field.
        OperatorAction {
            tick: 31,
            command: Command::ForcePoint {
                point: points::LEVEL_RAW,
                kind: ValueKind::Float,
                value: Value::Float(15.0),
            },
        },
        // The release, applied at scan 39: the next input phase reads
        // the field again.
        OperatorAction {
            tick: 39,
            command: Command::UnforcePoint {
                point: points::LEVEL_RAW,
            },
        },
        // The stop request, applied at scan 56; the feedback follows
        // at 58, inside `fault_ticks`.
        OperatorAction {
            tick: 56,
            command: Command::WriteValue {
                point: points::PUMP_START,
                kind: ValueKind::Bool,
                value: Value::Bool(false),
            },
        },
        // A mid-run retune through the parameter path, applied at 59.
        OperatorAction {
            tick: 59,
            command: Command::SetParameter {
                component: "pid:2".to_string(),
                name: "kp".to_string(),
                value: Value::Float(0.8),
            },
        },
    ]
}

/// Runs the scenario on the `sim-scripted` variant: the fixture's
/// declared script is the field side, so each boundary is only the
/// `FanoutDriver::step` the `--driven` wiring installs — the driven
/// step alone advances playback.
pub fn run_scripted() -> Result<VariantRun, TwoKindsError> {
    let (model, driver) = equivalence::local_variant(SCRIPTED_DOCUMENT, None)?;
    let driver = &driver;
    driven_run(&model, driver, TOTAL_SCANS, &actions(), move |boundary| {
        if boundary == 0 {
            return Ok(());
        }
        driver
            .step(SCAN_PERIOD)
            .map_err(|error| format!("plant step failed: {error}"))
    })
}

/// Binds a [`BusServer`] serving the bus fixture's declared register map
/// on an ephemeral port, substitutes its bound address for the
/// fixture's [`BUS_ADDRESS_PLACEHOLDER`], and returns the resolved model
/// with the server. The caller runs `server.serve()` on its own thread:
/// resolving the `sim-bus` device connects and probes every mapped
/// register, so the server must be serving before assembly runs.
pub fn bus_variant() -> Result<(PlantModel, BusServer), TwoKindsError> {
    // The register map is model data: the bank, the controller's
    // bindings, and the field feed all derive from the one fixture.
    equivalence::bus_variant(
        BUS_DOCUMENT,
        BUS_ADDRESS_PLACEHOLDER,
        BankDerivation::Neutral,
        None,
    )
}

/// Runs the scenario on the `sim-bus` variant: a [`BusServer`] serves
/// the model's declared register map on an ephemeral port, the
/// fixture's address placeholder is substituted, and a second
/// [`BusDriver`] attachment — the test's field side — writes each
/// [`FIELD_PROGRAM`] change's register inside the driven scan boundary.
/// No writer claim is taken in the run, so the device stays open to
/// both attachments.
pub fn run_bus() -> Result<VariantRun, TwoKindsError> {
    with_served_bank(
        BUS_DOCUMENT,
        BUS_ADDRESS_PLACEHOLDER,
        BankDerivation::Neutral,
        None,
        |model, addr| {
            let driver = resolve_drivers(model, &DriverRegistry::standard())?.build()?;
            let field = BusDriver::connect(addr, &field_bindings(model)?)?;
            let driver = &driver;
            let boundary = move |tick: u64| -> Result<(), String> {
                // The scan cycle's plant step — the same call the
                // `--driven` controller installs — then the field's
                // next presentation, still inside the request boundary.
                if tick > 0 {
                    driver
                        .step(SCAN_PERIOD)
                        .map_err(|error| format!("plant step failed: {error}"))?;
                }
                for change in FIELD_PROGRAM
                    .iter()
                    .filter(|change| change.tick == tick + 1)
                {
                    field.write(change.point, change.value).map_err(|error| {
                        format!("register feed for point {} failed: {error}", change.point.0)
                    })?;
                }
                Ok(())
            };
            driven_run(model, driver, TOTAL_SCANS, &actions(), boundary)
        },
    )
}
