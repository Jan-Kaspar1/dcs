//! Reusable, hardware-independent pump equipment composition.
//!
//! [`pump`] composes control, protection, diagnostics, managed alarms,
//! and equipment metadata from typed logical I/O. The caller owns device
//! bindings and automatic demand; no driver or duty/standby group is created.
//!
//! `mode=false` selects automatic demand; `mode=true` selects the manual
//! run request after the configured protection-clear holdout. Out-of-service,
//! power failure, thermal overload, moisture, dry-run, or untrusted protection
//! inputs inhibit both modes. Feedback disagreement raises a motor fault after
//! `motor_fault_ticks`; it clears automatically when feedback agrees again.
//! A feedback fault is diagnostic; withdrawing the automatic demand belongs to
//! an external allocator. It does not independently latch the pump stopped.
//! Alarm acknowledgment clears the unacknowledged latch and does not reset protection
//! or stop a standing run request. Clearing protection can therefore resume a
//! standing demand; use Stop request or Out of service before investigating.
//!
//! Automatic allocation availability reports auto/in-service with healthy
//! power, thermal and moisture inputs. The separate fault and protection
//! states must also be observed; availability alone is not permission to run.
//! Each composed block uses the runtime's normal checkpoint and scan semantics.

use crate::specs::{
    BoolGateSpec, DigitalInputSpec, InterlockSpec, ManagedAlarmHandles,
    ManagedBoolLatchingAlarmSpec, ManagedInputs, MotorSpec, TimerSpec,
};
use crate::{
    BuildError, InPoint, OutPoint, PlantBuilder, PointId, SignalId, Sink, Source, Value,
    parameters, unit,
};
use dcs_model::{ComponentId, Equipment, EquipmentControl, Rationalization};

const GATE_AND: i64 = 0;
const GATE_OR: i64 = 1;

/// Engineered identity, point allocation, and timing of one pump.
///
/// Point ids `point_base..point_base+32` are reserved for control wiring;
/// `alarm_base..alarm_base+30` are reserved for three managed alarms. The
/// caller must keep these regions clear of its own points and other pumps.
#[derive(Debug, Clone)]
pub struct PumpConfig {
    /// Stable equipment identity, independent of the component allocation order.
    pub id: String,
    /// Prefix of the pump's signals, e.g. `p101`.
    pub tag: String,
    /// Operator-facing equipment name.
    pub label: String,
    /// Monitoring group for the pump's declared signals.
    pub group: String,
    /// Start of the 32-point control allocation, at most `u64::MAX - 31`.
    pub point_base: u64,
    /// Start of the 30-point managed-alarm allocation, at most `u64::MAX - 29`.
    pub alarm_base: u64,
    /// Offset added to each point id for its signal id.
    pub signal_base: u64,
    /// Nonnegative consecutive protection-clear ticks before a manual start.
    pub min_off_ticks: i64,
    /// Nonnegative feedback-disagreement ticks before a motor fault.
    pub motor_fault_ticks: i64,
    /// Unit of the protection measurement, or `None` for an undeclared anchor.
    pub measurement_unit: Option<String>,
}

impl PumpConfig {
    /// Creates a pump with 3-tick manual holdout and 10-tick feedback budget.
    pub fn new(tag: impl Into<String>, point_base: u64, alarm_base: u64) -> Self {
        let tag = tag.into();
        Self {
            id: tag.clone(),
            label: tag.clone(),
            group: format!("pump-{tag}"),
            tag,
            point_base,
            alarm_base,
            signal_base: 10_000,
            min_off_ticks: 3,
            motor_fault_ticks: 10,
            measurement_unit: Some(unit::M.to_string()),
        }
    }
}

/// Optional feedback destinations in a surrounding process assembly.
///
/// These are ordinary typed ports, so the pump has no dependency on a group
/// kind. The returned handles can also be connected by the caller later.
#[derive(Debug, Clone, Default)]
pub struct PumpLinks {
    /// Run feedback consumed by an external allocator.
    pub run: Option<Sink<bool>>,
    /// Proven motor fault consumed by an external allocator.
    pub fault: Option<Sink<bool>>,
    /// Automatic allocation availability consumed by an external allocator.
    pub available: Option<Sink<bool>>,
    /// Manual-mode state consumed by an external process aggregate.
    pub manual: Option<Sink<bool>>,
}

/// Logical process and field bindings required by the pump.
#[derive(Debug, Clone)]
pub struct PumpInputs {
    /// Field run feedback. It is independent of the field command.
    pub run: InPoint<bool>,
    /// Thermal-overload contact (`true` means tripped).
    pub thermal: InPoint<bool>,
    /// Moisture-ingress contact (`true` means tripped).
    pub moisture: InPoint<bool>,
    /// Field run output driven by the composed motor.
    pub command: OutPoint<bool>,
    /// Automatic run demand from surrounding process logic.
    pub automatic_request: Source<bool>,
    /// Power-failure contact (`true` means tripped).
    pub power_fail: InPoint<bool>,
    /// Healthy-power carrier used for automatic allocation availability.
    pub power_ok: OutPoint<bool>,
    /// Process dry-run trip carrier (`true` means tripped).
    pub dry_run: OutPoint<bool>,
    /// Protective measurement whose untrusted quality also inhibits operation.
    pub protective_measurement: InPoint<f64>,
    /// Held good-quality analog anchor for individual contact cause alarms.
    pub guard_anchor: InPoint<f64>,
    /// Held true permissive for individual contact cause alarms.
    pub guard_true: InPoint<bool>,
    /// Optional surrounding assembly feedback connections.
    pub links: PumpLinks,
}

/// A managed alarm owned by equipment or a process assembly.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManagedAlarmLayout {
    /// The alarm component instance's id.
    pub component: ComponentId,
    /// The writable internal `In` point the operator ack lands on.
    pub ack: PointId,
    /// The point the `shelve` input binds — `Some` only where the
    /// instance declares the port. The point's `writable` flag is the
    /// declared shelving policy: a writable point carries the
    /// receipted, actor-attributed operator request; a read-only one
    /// is the never-shelvable declaration whose writes answer
    /// `NotWritable` at submission.
    pub shelve: Option<PointId>,
    /// The point the `oos` input binds — `Some` only where the
    /// instance declares the port: the operator's out-of-service
    /// command point, or the pump's own maintenance-inhibit point for
    /// the per-pump set.
    pub oos: Option<PointId>,
    /// The internal `Out` point carrying the standing `alarm` output.
    pub alarm: PointId,
    /// The internal `Out` point carrying the `unacknowledged` latch.
    pub unacknowledged: PointId,
    /// The internal `Out` point carrying the `shelved` status.
    pub shelved: PointId,
    /// The internal `Out` point carrying the `suppressed` status.
    pub suppressed: PointId,
    /// The internal `Out` point carrying the `out_of_service` status.
    pub out_of_service: PointId,
}

/// Point and component identities exposed by one complete pump.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PumpLayout {
    /// Field run command.
    pub cmd: PointId,
    /// Field run feedback.
    pub run: PointId,
    /// Thermal-overload contact.
    pub thermal: PointId,
    /// Moisture-ingress contact.
    pub moisture: PointId,
    /// Manual-mode request (`false` auto, `true` manual).
    pub mode: PointId,
    /// Manual run request.
    pub hand: PointId,
    /// Maintenance inhibit, active in both modes.
    pub out_of_service: PointId,
    /// Automatic demand carrier before mode selection and protection.
    pub automatic_request: PointId,
    /// Proven feedback-disagreement fault.
    pub fault: PointId,
    /// Automatic allocation availability.
    pub avail: PointId,
    /// Aggregated protection trip.
    pub protect_tripped: PointId,
    /// Protection-clear carrier.
    pub protections_ok: PointId,
    /// Delivered process dry-run trip.
    pub dry_run: PointId,
    /// Motor block instance.
    pub motor: ComponentId,
    /// Managed motor-fault alarm.
    pub fault_alarm: ManagedAlarmLayout,
    /// Managed thermal-overload alarm.
    pub thermal_alarm: ManagedAlarmLayout,
    /// Managed moisture-ingress alarm.
    pub moisture_alarm: ManagedAlarmLayout,
}

/// Typed handles returned to the surrounding process assembly.
#[derive(Debug, Clone)]
pub struct PumpInstance {
    /// Writable manual-mode request.
    pub mode: InPoint<bool>,
    /// Writable manual run request.
    pub hand: InPoint<bool>,
    /// Writable maintenance-inhibit state.
    pub out_of_service: InPoint<bool>,
    /// Automatic allocation availability delivered to a consumer.
    pub available: InPoint<bool>,
    /// Proven motor fault delivered to a consumer.
    pub fault: InPoint<bool>,
    /// Stable point layout and alarm identities.
    pub layout: PumpLayout,
}

/// Composes one reusable pump using existing logical I/O bindings.
///
/// The caller declares the field channels and any simulation feedback loop.
/// Automatic demand, duty rotation, and process thresholds remain outside.
/// [`PlantBuilder::build`] validates allocation collisions, timing ranges,
/// and the resulting model just as it validates primitive compositions.
/// Returns [`BuildError::InvalidConfiguration`] for negative timing or
/// overflowing point/signal allocations before changing the builder.
pub fn pump(
    plant: &mut PlantBuilder,
    config: &PumpConfig,
    inputs: PumpInputs,
) -> Result<PumpInstance, BuildError> {
    validate_config(config, &inputs)?;
    let tag = &config.tag;
    let group_name = config.group.clone();
    let base = config.point_base;
    let run = inputs.run;
    let thermal = inputs.thermal;
    let moisture = inputs.moisture;
    let cmd = inputs.command;
    let power_fail = inputs.power_fail;
    let power_ok = inputs.power_ok;
    let level_chain = inputs.protective_measurement;
    let below_cutoff = inputs.dry_run;
    let guard_anchor = inputs.guard_anchor;
    let guard_true = inputs.guard_true;
    plant.journaled(run);
    plant.journaled(thermal);
    plant.journaled(moisture);
    for (point, suffix, description) in [
        (run.id(), "run", "Run feedback contact"),
        (thermal.id(), "thermal", "Thermal-overload contact"),
        (moisture.id(), "moisture", "Moisture ingress contact"),
        (cmd.id(), "cmd", "Field run command"),
    ] {
        signal(
            plant,
            config.signal_base,
            point,
            &format!("{tag}-{suffix}"),
            "",
            description,
            &group_name,
        );
    }
    // Writable operator points: manual mode, hand request, out of
    // service. `mode` and `out_of_service` carry the durable record's
    // mode-change and managed-state transitions (decisions 74, 75);
    // `hand` is a demand request — its writes are already the
    // attributed receipts, so it stays off the journaled set.
    let mode = plant.internal_input::<bool>(PointId(base), false, true);
    let hand = plant.internal_input::<bool>(PointId(base + 1), false, true);
    let oos = plant.internal_input::<bool>(PointId(base + 2), false, true);
    plant.journaled(mode);
    plant.journaled(oos);
    signal(
        plant,
        config.signal_base,
        PointId(base),
        &format!("{tag}-mode"),
        "",
        "Manual takeover — false auto, true hand",
        &group_name,
    );
    signal(
        plant,
        config.signal_base,
        PointId(base + 1),
        &format!("{tag}-hand"),
        "",
        "Operator's hand run request while manual",
        &group_name,
    );
    signal(
        plant,
        config.signal_base,
        PointId(base + 2),
        &format!("{tag}-oos"),
        "",
        "Out of service — inhibits auto and hand operation",
        &group_name,
    );

    // Carriers and consumers: the group's cmd_i output, the inverted
    // mode, the inverted out-of-service, station power-ok, and the
    // motor's fault flag each fan out through an `Out`/`In` pair per
    // consumer.
    let group_cmd = plant.internal_output::<bool>(PointId(base + 3), false);
    let group_cmd_in = plant.internal_input::<bool>(PointId(base + 4), false, false);
    // The `auto`/`oos-ok`/`power-ok` carriers seed `true` — the
    // cold-start image reads as in-auto, in-service, powered until the
    // first computed values land, so `avail_i` and the guard don't
    // flicker the pump out for a scan.
    let auto = plant.internal_output::<bool>(PointId(base + 5), true);
    let auto_leg_in = plant.internal_input::<bool>(PointId(base + 6), false, false);
    let auto_avail_in = plant.internal_input::<bool>(PointId(base + 7), false, false);
    let oos_ok = plant.internal_output::<bool>(PointId(base + 8), true);
    let oos_ok_avail_in = plant.internal_input::<bool>(PointId(base + 9), false, false);
    let oos_ok_guard_in = plant.internal_input::<bool>(PointId(base + 10), false, false);
    let power_ok_in = plant.internal_input::<bool>(PointId(base + 11), false, false);
    let fault = plant.internal_output::<bool>(PointId(base + 12), false);
    let fault_group_in = plant.internal_input::<bool>(PointId(base + 13), false, false);
    let fault_alarm_in = plant.internal_input::<bool>(PointId(base + 14), false, false);
    // The healthy-contact and aggregated-availability carriers — seeded
    // `true` like `auto`/`oos-ok` so scan 1 reads the declared cold-start
    // state, not a transient trip.
    let thermal_ok = plant.internal_output::<bool>(PointId(base + 24), true);
    let thermal_ok_in = plant.internal_input::<bool>(PointId(base + 25), false, false);
    let moisture_ok = plant.internal_output::<bool>(PointId(base + 26), true);
    let moisture_ok_in = plant.internal_input::<bool>(PointId(base + 27), false, false);
    let avail_carrier = plant.internal_output::<bool>(PointId(base + 28), true);
    let avail_in = plant.internal_input::<bool>(PointId(base + 29), false, false);
    // Decision 88's protection carriers: the dry-run flag's delivered
    // copy, the interlock's `tripped` and pass-through, and the
    // inverted `protections-ok` pair the command guard and the hand
    // holdout read. `protections-ok` seeds `true` like the other
    // healthy-state carriers — cold start reads protected until the
    // first computed values land.
    let below_cutoff_in = plant.internal_input::<bool>(PointId(base + 15), false, false);
    let protect_tripped = plant.internal_output::<bool>(PointId(base + 16), false);
    let protect_tripped_in = plant.internal_input::<bool>(PointId(base + 17), false, false);
    let protections_ok = plant.internal_output::<bool>(PointId(base + 18), true);
    let protections_ok_in = plant.internal_input::<bool>(PointId(base + 19), false, false);
    let protect_out = plant.internal_output::<f64>(PointId(base + 20), 0.0);
    // The cause guards' pass-through carriers — unused like
    // `protect-out`: each guard exists for its `tripped` flag.
    let thermal_guard_out = plant.internal_output::<f64>(PointId(base + 21), 0.0);
    let moisture_guard_out = plant.internal_output::<f64>(PointId(base + 22), 0.0);
    // The proven fault, the aggregated availability, and the
    // protection state are protection-relevant status — `journaled`
    // like the contacts feeding them; the inverted and delivered
    // copies stay off the record, their sources already carry it.
    plant.journaled(fault);
    plant.journaled(avail_carrier);
    plant.journaled(protect_tripped);
    plant.journaled(protections_ok);
    for (point, name, description) in [
        (
            base + 3,
            "group-cmd",
            "The pump group's automatic run request",
        ),
        (
            base + 4,
            "group-cmd-in",
            "Group request delivered to the auto leg",
        ),
        (base + 5, "auto", "In auto — the inverted manual-mode point"),
        (base + 6, "auto-leg-in", "In-auto delivered to the auto leg"),
        (
            base + 7,
            "auto-avail-in",
            "In-auto delivered to availability",
        ),
        (
            base + 8,
            "oos-ok",
            "In service — the inverted out-of-service point",
        ),
        (
            base + 9,
            "oos-ok-avail-in",
            "In-service delivered to availability",
        ),
        (
            base + 10,
            "oos-ok-guard-in",
            "In-service delivered to the protection permissive",
        ),
        (
            base + 11,
            "power-ok-in",
            "Station power-ok delivered to availability",
        ),
        (
            base + 12,
            "fault",
            "The motor's proven command/feedback fault",
        ),
        (
            base + 13,
            "fault-group-in",
            "Motor fault delivered to the pump group",
        ),
        (
            base + 14,
            "fault-alarm-in",
            "Motor fault delivered to the alarm",
        ),
        (
            base + 15,
            "below-cutoff-in",
            "Dry-run cutoff delivered to the protection interlock",
        ),
        (
            base + 16,
            "protect-tripped",
            "Protection interlock tripped — a condition asserted or untrusted",
        ),
        (
            base + 17,
            "protect-tripped-in",
            "Protection trip delivered to its inversion",
        ),
        (
            base + 18,
            "protections-ok",
            "Protections clear and trusted — the inverted interlock trip",
        ),
        (
            base + 19,
            "protections-ok-in",
            "Protections-clear delivered to the guard and holdout",
        ),
        (
            base + 20,
            "protect-out",
            "The protection interlock's analog pass-through — unused",
        ),
        (
            base + 21,
            "thermal-guard-out",
            "The thermal cause guard's analog pass-through — unused",
        ),
        (
            base + 22,
            "moisture-guard-out",
            "The moisture cause guard's analog pass-through — unused",
        ),
        (
            base + 24,
            "thermal-ok",
            "Thermal contact healthy — the inverted contact",
        ),
        (
            base + 25,
            "thermal-ok-in",
            "Thermal-healthy delivered to availability",
        ),
        (
            base + 26,
            "moisture-ok",
            "Moisture contact healthy — the inverted contact",
        ),
        (
            base + 27,
            "moisture-ok-in",
            "Moisture-healthy delivered to availability",
        ),
        (
            base + 28,
            "avail",
            "Aggregated availability for the pump group",
        ),
        (
            base + 29,
            "avail-in",
            "Availability delivered to the pump group",
        ),
    ] {
        signal(
            plant,
            config.signal_base,
            PointId(point),
            &format!("{tag}-{name}"),
            "",
            description,
            &group_name,
        );
    }

    // The availability aggregation and the manual-takeover gates.
    let invert = || parameters([("invert", Value::Bool(true))]);
    let inv_mode = plant.add(DigitalInputSpec::new(invert()));
    let inv_oos = plant.add(DigitalInputSpec::new(invert()));
    let inv_thermal = plant.add(DigitalInputSpec::new(invert()));
    let inv_moisture = plant.add(DigitalInputSpec::new(invert()));
    let avail = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_AND))]),
        5,
    ));
    let auto_leg = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_AND))]),
        2,
    ));
    let hand_leg = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_AND))]),
        3,
    ));
    let select = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_OR))]),
        2,
    ));
    let guard = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_AND))]),
        2,
    ));
    // Decision 88's protection aggregation: the `interlock` trips on
    // an asserted *or* untrusted condition alike — the thermal,
    // moisture, and power-fail contacts and the chain's `below_cutoff`
    // as its trips, in-service as its permissive, and the delivered
    // selected level on its `in` so an untrusted measurement cannot
    // prove the dry-run trip clear. The inverted `tripped` guards the
    // command in both modes, and the `timer` at `min_off_ticks` holds
    // the hand leg out until the protections have stood — the group's
    // `min_off`/`restage` discipline reaches only the auto leg.
    let protect = plant.add(InterlockSpec::new(
        parameters([("safe_value", Value::Float(0.0))]),
        4,
    ));
    // The cause guards — decision 88's annunciation half for the
    // per-pump contacts, the station power guard's shape repeated per
    // cause: a held `Good` `in` and `permissive` leave `tripped`
    // reporting the contact alone, asserted or untrusted alike. Each
    // contact alarm binds its guard's `tripped` directly, so a
    // degraded contact can no longer trip the pump while its cause
    // alarm stays clean (issue #809).
    let thermal_guard = plant.add(InterlockSpec::new(
        parameters([("safe_value", Value::Float(0.0))]),
        1,
    ));
    let moisture_guard = plant.add(InterlockSpec::new(
        parameters([("safe_value", Value::Float(0.0))]),
        1,
    ));
    let inv_protect = plant.add(DigitalInputSpec::new(invert()));
    let holdout = plant.add(TimerSpec::new(parameters([(
        "delay_ticks",
        Value::Int(config.min_off_ticks),
    )])));
    let motor = plant.add(MotorSpec::new(parameters([(
        "fault_ticks",
        Value::Int(config.motor_fault_ticks),
    )])));
    // The pump's three alarms, all managed. The fault alarm declares
    // `oos`/`suppress` — both bound below to the pump's own
    // out-of-service point, so the maintenance-inhibit state doubles
    // as the designed-suppression condition: a deliberately offline
    // pump's fault stays named and countable without annunciating
    // (decision 73). The contact alarms declare no lifecycle inputs —
    // never-shelvable with no shelving surface, never suppressed,
    // never out of service; their managed status outputs still
    // report.
    let fault_alarm = plant.add(ManagedBoolLatchingAlarmSpec::new(
        parameters([
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(2)),
            ("class", Value::Int(2)),
            ("response_ticks", Value::Int(60)),
        ]),
        ManagedInputs {
            oos: true,
            suppress: true,
            ..ManagedInputs::default()
        },
        rationalization(
            "Command and running feedback disagree; operation is not confirmed",
            "Stop the request or take the pump out of service, then inspect the motor and feedback",
            &format!("{tag}-fault-alarm"),
        ),
    ));
    let thermal_alarm = plant.add(ManagedBoolLatchingAlarmSpec::new(
        parameters([
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(2)),
            ("class", Value::Int(2)),
            ("response_ticks", Value::Int(60)),
        ]),
        ManagedInputs::default(),
        rationalization(
            "The motor overheats and the pump trips out",
            "Investigate the thermal overload and reset the contact",
            &format!("{tag}-thermal-alarm"),
        ),
    ));
    let moisture_alarm = plant.add(ManagedBoolLatchingAlarmSpec::new(
        parameters([
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(3)),
            ("class", Value::Int(2)),
            ("response_ticks", Value::Int(60)),
        ]),
        ManagedInputs::default(),
        rationalization(
            "Water ingress degrades the motor insulation",
            "Schedule a seal inspection for the pump",
            &format!("{tag}-moisture-alarm"),
        ),
    ));

    // Mode and service inversions; the group request carrier.
    //
    // The per-pump dimensional contract: the protection `interlock`
    // gates the selected level, so its `in`/`out` and its `safe_value`
    // are `m` — the closed-valve safe level. The two cause guards stay
    // undeclared beside it, their analog feeds being held anchors
    // rather than process quantities. The `timer`'s holdout interval and
    // the `motor`'s feedback-disagreement budget are the kinds' own
    // `ticks`, and each pump's three managed alarms declare their
    // shelving bound and response budget.
    if let Some(measurement_unit) = &config.measurement_unit {
        plant.port_unit(protect.id, "in", measurement_unit);
        plant.port_unit(protect.id, "out", measurement_unit);
        plant.param_unit(protect.id, "safe_value", measurement_unit);
    }
    plant.param_unit(holdout.id, "delay_ticks", unit::TICKS);
    plant.param_unit(motor.id, "fault_ticks", unit::TICKS);
    for alarm in [fault_alarm.id, thermal_alarm.id, moisture_alarm.id] {
        for parameter in ["max_shelve_ticks", "response_ticks"] {
            plant.param_unit(alarm, parameter, unit::TICKS);
        }
    }

    plant.connect(mode, &inv_mode.input);
    plant.connect(oos, &inv_oos.input);
    plant.connect(thermal, &inv_thermal.input);
    plant.connect(moisture, &inv_moisture.input);
    plant.connect(&inputs.automatic_request, group_cmd);
    plant.connect(group_cmd_in, group_cmd);
    plant.connect(&inv_mode.out, auto);
    plant.connect(auto_leg_in, auto);
    plant.connect(auto_avail_in, auto);
    plant.connect(&inv_oos.out, oos_ok);
    plant.connect(oos_ok_avail_in, oos_ok);
    plant.connect(oos_ok_guard_in, oos_ok);
    plant.connect(power_ok_in, power_ok);

    // avail_i = in-auto and in-service and power-ok and thermal-ok and
    // moisture-ok — decision 41's aggregated availability, delivered to
    // the group through its own seeded carrier pair.
    plant.connect(&inv_thermal.out, thermal_ok);
    plant.connect(thermal_ok_in, thermal_ok);
    plant.connect(&inv_moisture.out, moisture_ok);
    plant.connect(moisture_ok_in, moisture_ok);
    plant.connect(auto_avail_in, avail.input(1));
    plant.connect(oos_ok_avail_in, avail.input(2));
    plant.connect(power_ok_in, avail.input(3));
    plant.connect(thermal_ok_in, avail.input(4));
    plant.connect(moisture_ok_in, avail.input(5));
    plant.connect(&avail.out, avail_carrier);
    plant.connect(avail_in, avail_carrier);
    if let Some(destination) = &inputs.links.available {
        plant.connect(avail_in, destination);
    }

    // Decision 88's protection aggregation: the raw contacts bind the
    // interlock's trips directly — a point may feed many port inputs —
    // so an asserted *or* untrusted thermal, moisture, power-fail, or
    // dry-run condition trips the pump; `oos` stands as the permissive
    // and the delivered selected level on `in` fails the same way. The
    // inverted `tripped` is `protections-ok`; the `timer` holds the
    // hand leg out for `min_off_ticks` after the protections clear.
    plant.connect(below_cutoff_in, below_cutoff);
    plant.connect(level_chain, &protect.input);
    plant.connect(oos_ok_guard_in, &protect.permissive);
    plant.connect(thermal, protect.trip(1));
    plant.connect(moisture, protect.trip(2));
    plant.connect(power_fail, protect.trip(3));
    plant.connect(below_cutoff_in, protect.trip(4));
    plant.connect(&protect.out, protect_out);
    plant.connect(&protect.tripped, protect_tripped);
    plant.connect(protect_tripped_in, protect_tripped);
    plant.connect(protect_tripped_in, &inv_protect.input);
    plant.connect(&inv_protect.out, protections_ok);
    plant.connect(protections_ok_in, protections_ok);
    plant.connect(protections_ok_in, &holdout.input);

    // The per-pump cause guards: one `interlock` per contact whose
    // `tripped` feeds the cause alarm's condition — the port-to-port
    // wire synthesizes the delivered copy, so the asserted *or*
    // untrusted contact annunciates on the same reading that trips
    // the pump.
    plant.connect(guard_anchor, &thermal_guard.input);
    plant.connect(guard_true, &thermal_guard.permissive);
    plant.connect(thermal, thermal_guard.trip(1));
    plant.connect(&thermal_guard.out, thermal_guard_out);
    plant.connect(&thermal_guard.tripped, &thermal_alarm.input);
    plant.connect(guard_anchor, &moisture_guard.input);
    plant.connect(guard_true, &moisture_guard.permissive);
    plant.connect(moisture, moisture_guard.trip(1));
    plant.connect(&moisture_guard.out, moisture_guard_out);
    plant.connect(&moisture_guard.tripped, &moisture_alarm.input);

    // The manual-takeover shape under decision 88: `motor.cmd =
    // ((group cmd and not mode) or (hand and mode and the held
    // protection set)) and protections-ok` — the operator's `hand`
    // request stays a demand the declared protections bound, not a
    // bypass. A pump held in manual feeds the `any-manual`
    // aggregation the `none-available` alarm suppresses on.
    plant.connect(group_cmd_in, auto_leg.input(1));
    plant.connect(auto_leg_in, auto_leg.input(2));
    plant.connect(hand, hand_leg.input(1));
    plant.connect(mode, hand_leg.input(2));
    plant.connect(&holdout.out, hand_leg.input(3));
    plant.connect(&auto_leg.out, select.input(1));
    plant.connect(&hand_leg.out, select.input(2));
    plant.connect(&select.out, guard.input(1));
    plant.connect(protections_ok_in, guard.input(2));
    plant.connect(&guard.out, &motor.cmd);
    if let Some(destination) = &inputs.links.manual {
        plant.connect(mode, destination);
    }
    plant.connect(run, &motor.run);
    plant.connect(&motor.out, cmd);

    // The group's run/fault feedback.
    if let Some(destination) = &inputs.links.run {
        plant.connect(run, destination);
    }
    plant.connect(&motor.fault, fault);
    plant.connect(fault_group_in, fault);
    if let Some(destination) = &inputs.links.fault {
        plant.connect(fault_group_in, destination);
    }
    plant.connect(fault_alarm_in, fault);

    // The pump's three managed alarms — motor fault, thermal contact,
    // moisture contact — laid out in the alarm region at
    // `config.alarm_basendex + offset`, each on its own writable
    // ack. The fault alarm's `oos` binds the pump's `oos` point — the
    // same writable maintenance-inhibit point the operator commands —
    // directly, while its `suppress` reads the pass-through copy a
    // `digital-input` composes: a component binds each point once, so
    // the same declared state reaches the second input through the
    // carrier pair one scan later.
    let oos_copy = plant.add(DigitalInputSpec::new(parameters([(
        "invert",
        Value::Bool(false),
    )])));
    let fault_sup = plant.internal_output::<bool>(PointId(base + 30), false);
    let fault_sup_in = plant.internal_input::<bool>(PointId(base + 31), false, false);
    plant.connect(oos, &oos_copy.input);
    plant.connect(&oos_copy.out, fault_sup);
    plant.connect(fault_sup_in, fault_sup);
    signal(
        plant,
        config.signal_base,
        PointId(base + 30),
        &format!("{tag}-fault-sup"),
        "",
        "Out-of-service state delivered to the fault alarm's suppression",
        &group_name,
    );
    signal(
        plant,
        config.signal_base,
        PointId(base + 31),
        &format!("{tag}-fault-sup-in"),
        "",
        "The fault alarm's suppression condition",
        &group_name,
    );
    let fault_alarm_layout = pump_alarm(
        plant,
        config.signal_base,
        config.alarm_base,
        fault_alarm.id,
        &fault_alarm.ack,
        &fault_alarm.managed,
        &fault_alarm.alarm,
        &fault_alarm.unacknowledged,
        oos,
        fault_sup_in,
        &format!("{tag}-fault"),
        &group_name,
    );
    plant.connect(fault_alarm_in, &fault_alarm.input);
    let thermal_alarm_layout = pump_alarm(
        plant,
        config.signal_base,
        config.alarm_base + 10,
        thermal_alarm.id,
        &thermal_alarm.ack,
        &thermal_alarm.managed,
        &thermal_alarm.alarm,
        &thermal_alarm.unacknowledged,
        oos,
        fault_sup_in,
        &format!("{tag}-thermal"),
        &group_name,
    );
    let moisture_alarm_layout = pump_alarm(
        plant,
        config.signal_base,
        config.alarm_base + 20,
        moisture_alarm.id,
        &moisture_alarm.ack,
        &moisture_alarm.managed,
        &moisture_alarm.alarm,
        &moisture_alarm.unacknowledged,
        oos,
        fault_sup_in,
        &format!("{tag}-moisture"),
        &group_name,
    );

    let layout = PumpLayout {
        cmd: cmd.id(),
        run: run.id(),
        thermal: thermal.id(),
        moisture: moisture.id(),
        mode: mode.id(),
        hand: hand.id(),
        out_of_service: oos.id(),
        automatic_request: group_cmd.id(),
        fault: fault.id(),
        avail: avail_carrier.id(),
        protect_tripped: protect_tripped.id(),
        protections_ok: protections_ok.id(),
        dry_run: below_cutoff_in.id(),
        motor: motor.id,
        fault_alarm: fault_alarm_layout,
        thermal_alarm: thermal_alarm_layout,
        moisture_alarm: moisture_alarm_layout,
    };
    let components = vec![
        inv_mode.id,
        inv_oos.id,
        inv_thermal.id,
        inv_moisture.id,
        avail.id,
        auto_leg.id,
        hand_leg.id,
        select.id,
        guard.id,
        protect.id,
        thermal_guard.id,
        moisture_guard.id,
        inv_protect.id,
        holdout.id,
        motor.id,
        fault_alarm.id,
        thermal_alarm.id,
        moisture_alarm.id,
        oos_copy.id,
    ];
    let mut points = vec![
        layout.run,
        layout.cmd,
        layout.mode,
        layout.hand,
        layout.out_of_service,
        layout.automatic_request,
        layout.avail,
        layout.fault,
        layout.protect_tripped,
        layout.protections_ok,
        layout.thermal,
        layout.moisture,
        layout.dry_run,
    ];
    for alarm in [
        &layout.fault_alarm,
        &layout.thermal_alarm,
        &layout.moisture_alarm,
    ] {
        points.extend([
            alarm.ack,
            alarm.alarm,
            alarm.unacknowledged,
            alarm.shelved,
            alarm.suppressed,
            alarm.out_of_service,
        ]);
    }
    plant.equipment(Equipment {
        id: config.id.clone(),
        label: config.label.clone(),
        kind: "pump".to_string(),
        components,
        points,
        controls: vec![
            EquipmentControl {
                point: mode.id(),
                label: "Operating mode".to_string(),
                false_label: Some("Auto".to_string()),
                true_label: Some("Manual".to_string()),
            },
            EquipmentControl {
                point: hand.id(),
                label: "Manual run request".to_string(),
                false_label: Some("Stop request".to_string()),
                true_label: Some("Run request".to_string()),
            },
            EquipmentControl {
                point: oos.id(),
                label: "Service state".to_string(),
                false_label: Some("In service".to_string()),
                true_label: Some("Out of service".to_string()),
            },
        ],
    });
    Ok(PumpInstance {
        mode,
        hand,
        out_of_service: oos,
        available: avail_in,
        fault: fault_group_in,
        layout,
    })
}

fn validate_config(config: &PumpConfig, inputs: &PumpInputs) -> Result<(), BuildError> {
    let invalid = |field: &str, reason: &str| BuildError::InvalidConfiguration {
        field: field.to_string(),
        reason: reason.to_string(),
    };
    for (field, ticks) in [
        ("min_off_ticks", config.min_off_ticks),
        ("motor_fault_ticks", config.motor_fault_ticks),
    ] {
        if ticks < 0 {
            return Err(invalid(field, "tick budget must be nonnegative"));
        }
    }
    let control_end = config
        .point_base
        .checked_add(31)
        .ok_or_else(|| invalid("point_base", "32-point allocation exceeds u64::MAX"))?;
    let alarm_end = config
        .alarm_base
        .checked_add(29)
        .ok_or_else(|| invalid("alarm_base", "30-point allocation exceeds u64::MAX"))?;
    for point in [
        control_end,
        alarm_end,
        inputs.run.id().0,
        inputs.thermal.id().0,
        inputs.moisture.id().0,
        inputs.command.id().0,
    ] {
        if config.signal_base.checked_add(point).is_none() {
            return Err(invalid(
                "signal_base",
                "signal offset plus point id exceeds u64::MAX",
            ));
        }
    }
    Ok(())
}

fn rationalization(consequence: &str, required_action: &str, reference: &str) -> Rationalization {
    Rationalization {
        consequence: consequence.to_string(),
        required_action: required_action.to_string(),
        reference: reference.to_string(),
    }
}

#[allow(clippy::too_many_arguments)]
fn signal(
    plant: &mut PlantBuilder,
    signal_base: u64,
    point: PointId,
    name: &str,
    unit: &str,
    description: &str,
    group: &str,
) {
    if unit.is_empty() {
        plant
            .signal(SignalId(signal_base + point.0), name, point)
            .unit(unit)
            .description(description)
            .group(group);
    } else {
        plant.unit(point, unit);
        plant
            .signal(SignalId(signal_base + point.0), name, point)
            .description(description)
            .group(group);
    }
}

/// Declares one per-pump managed alarm's points — the writable `ack`
/// and the five status outputs — wires them, and binds the declared
/// `oos`/`suppress` inputs to the pump's maintenance-inhibit state:
/// `oos` reads `inhibit` — the pump's own writable out-of-service
/// point — while `suppress` reads `suppress_in`, the delivered copy
/// `pump` composes, since a component binds each point once. A
/// fault alarm on a deliberately offline machine stays named and
/// countable without annunciating — decision 73's station wiring. The
/// caller wires the alarm's `in` port.
#[allow(clippy::too_many_arguments)]
fn pump_alarm(
    plant: &mut PlantBuilder,
    signal_base: u64,
    base: u64,
    component: ComponentId,
    ack_port: &Sink<bool>,
    managed: &ManagedAlarmHandles,
    alarm_port: &Source<bool>,
    unacknowledged_port: &Source<bool>,
    inhibit: InPoint<bool>,
    suppress_in: InPoint<bool>,
    prefix: &str,
    group: &str,
) -> ManagedAlarmLayout {
    let ack = plant.internal_input::<bool>(PointId(base), false, true);
    let alarm = plant.internal_output::<bool>(PointId(base + 3), false);
    let unacknowledged = plant.internal_output::<bool>(PointId(base + 4), false);
    let shelved = plant.internal_output::<bool>(PointId(base + 5), false);
    let suppressed = plant.internal_output::<bool>(PointId(base + 6), false);
    let out_of_service = plant.internal_output::<bool>(PointId(base + 7), false);

    // The same decision-74 lifecycle audit as the station set: all five
    // status points are `journaled`.
    for point in [alarm, unacknowledged, shelved, suppressed, out_of_service] {
        plant.journaled(point);
    }

    plant.connect(ack, ack_port);
    if let Some(port) = managed.oos.as_ref() {
        plant.connect(inhibit, port);
    }
    if let Some(port) = managed.suppress.as_ref() {
        plant.connect(suppress_in, port);
    }
    plant.connect(alarm_port, alarm);
    plant.connect(unacknowledged_port, unacknowledged);
    plant.connect(&managed.shelved, shelved);
    plant.connect(&managed.suppressed, suppressed);
    plant.connect(&managed.out_of_service, out_of_service);

    signal(
        plant,
        signal_base,
        PointId(base),
        &format!("{prefix}-ack"),
        "",
        "Operator acknowledgment for the alarm",
        group,
    );
    for (offset, suffix, description) in [
        (
            3,
            "alarm",
            "Standing alarm state — process truth under every managed flag",
        ),
        (
            4,
            "unacknowledged",
            "Latched until the operator acknowledges",
        ),
        (5, "shelved", "Shelved within the declared bound"),
        (
            6,
            "suppressed",
            "Suppressed while the pump is out of service",
        ),
        (
            7,
            "out-of-service",
            "Out of service with the pump's maintenance state",
        ),
    ] {
        signal(
            plant,
            signal_base,
            PointId(base + offset),
            &format!("{prefix}-{suffix}"),
            "",
            description,
            group,
        );
    }
    ManagedAlarmLayout {
        component,
        ack: PointId(base),
        shelve: None,
        oos: managed.oos.as_ref().map(|_| inhibit.into()),
        alarm: PointId(base + 3),
        unacknowledged: PointId(base + 4),
        shelved: PointId(base + 5),
        suppressed: PointId(base + 6),
        out_of_service: PointId(base + 7),
    }
}
