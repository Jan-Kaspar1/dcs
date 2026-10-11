//! Standard equipment compositions over the existing runtime block contracts.
//! Logical bindings are supplied by the plant. Simulation and device selection
//! stay with the customer; equipment registers its controls, diagnostics and
//! managed alarms in the same model consumed by the generic monitor.

use crate::specs::{
    AnalogInputSpec, BoolGateSpec, DigitalInputSpec, InterlockSpec, ManagedBoolLatchingAlarmSpec,
    ManagedInputs, ManagedLatchingAlarmSpec, ManualStationSpec, MotorSpec, PidSpec, ValveSpec,
};
use crate::{
    ComponentId, Equipment, EquipmentControl, InPoint, OutPoint, PlantBuilder, PointId, SignalId,
    Source, Value, parameters,
};

/// Stable identity and disjoint allocation for a reusable equipment instance.
/// Reserve `base..base+100` for control and alarm points.
#[derive(Debug, Clone)]
pub struct EquipmentConfig {
    /// Stable identity, also the signal prefix.
    pub id: String,
    /// Operator-facing label.
    pub label: String,
    /// First point of the 100-point allocation.
    pub base: u64,
}

impl EquipmentConfig {
    fn validate(&self) -> Result<(), crate::BuildError> {
        if self.id.trim().is_empty() || self.base.checked_add(100_099).is_none() {
            return Err(crate::BuildError::InvalidConfiguration {
                field: "equipment identity/allocation".into(),
                reason: "nonempty identity and nonoverflowing 100-point/signal allocation required"
                    .into(),
            });
        }
        Ok(())
    }
    /// Declares an instance; model validation detects overlapping allocations.
    pub fn new(id: impl Into<String>, base: u64) -> Self {
        let id = id.into();
        Self {
            label: id.clone(),
            id,
            base,
        }
    }
}

fn signal(
    plant: &mut PlantBuilder,
    config: &EquipmentConfig,
    id: PointId,
    suffix: &str,
    unit: &str,
    description: &str,
) {
    // The fixed offset separates public display signals from station signals.
    plant
        .signal(
            SignalId(100_000 + id.0),
            &format!("{}-{suffix}", config.id),
            id,
        )
        .unit(unit)
        .group(&config.id)
        .description(description);
}

fn input<T: crate::PointType>(
    plant: &mut PlantBuilder,
    c: &EquipmentConfig,
    offset: u64,
    (initial, writable): (T, bool),
    suffix: &str,
    unit: &str,
    description: &str,
) -> InPoint<T> {
    let point = plant.internal_input(PointId(c.base + offset), initial, writable);
    plant.unit(point, unit);
    signal(plant, c, point.id(), suffix, unit, description);
    point
}

fn output<T: crate::PointType>(
    plant: &mut PlantBuilder,
    c: &EquipmentConfig,
    offset: u64,
    initial: T,
    suffix: &str,
    unit: &str,
    description: &str,
) -> OutPoint<T> {
    let point = plant.internal_output(PointId(c.base + offset), initial);
    plant.unit(point, unit);
    signal(plant, c, point.id(), suffix, unit, description);
    point
}

fn control(
    point: PointId,
    role: dcs_model::EquipmentControlRole,
    label: &str,
    levels: Option<(&str, &str)>,
) -> EquipmentControl {
    EquipmentControl {
        role: Some(role),
        limits: None,
        point,
        label: label.into(),
        false_label: levels.map(|v| v.0.into()),
        true_label: levels.map(|v| v.1.into()),
    }
}

fn bounded_control(
    point: PointId,
    role: dcs_model::EquipmentControlRole,
    label: &str,
    [min, max]: [f64; 2],
) -> EquipmentControl {
    let mut control = control(point, role, label, None);
    control.limits = Some(dcs_core::ParameterRange {
        min: Value::Float(min),
        max: Value::Float(max),
    });
    control
}

fn fault_alarm(
    plant: &mut PlantBuilder,
    c: &EquipmentConfig,
    fault: OutPoint<bool>,
    offset: u64,
    members: &mut Vec<ComponentId>,
    points: &mut Vec<PointId>,
) {
    let condition = input(
        plant,
        c,
        offset,
        (false, false),
        "fault-condition",
        "",
        "Feedback fault condition",
    );
    plant.connect(condition, fault);
    let alarm = plant.add(ManagedBoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(300)),
            ("max_shelve_ticks", Value::Int(0)),
        ]),
        ManagedInputs::default(),
        crate::Rationalization {
            consequence: "Requested actuator state is not proven by physical feedback".into(),
            required_action:
                "Inspect actuator and feedback; isolate the equipment before maintenance".into(),
            reference: c.id.clone(),
        },
    ));
    plant.connect(condition, &alarm.input);
    let ack = input(
        plant,
        c,
        offset + 1,
        (false, true),
        "ack",
        "",
        "Acknowledge feedback alarm; does not reset protection",
    );
    plant.connect(ack, &alarm.ack);
    for (delta, suffix, port) in [
        (2, "alarm", &alarm.alarm),
        (3, "unacknowledged", &alarm.unacknowledged),
        (4, "shelved", &alarm.managed.shelved),
        (5, "suppressed", &alarm.managed.suppressed),
        (6, "out-of-service", &alarm.managed.out_of_service),
    ] {
        let point = output(
            plant,
            c,
            offset + delta,
            false,
            suffix,
            "",
            "Managed feedback alarm state",
        );
        plant.connect(port, point);
        plant.journaled(point);
        points.push(point.id());
    }
    points.push(ack.id());
    members.push(alarm.id);
}

/// An isolation valve with one end-position feedback contact.
#[derive(Debug, Clone)]
pub struct OnOffValve {
    /// Writable opening request; admission means stored demand, not open proof.
    pub request: InPoint<bool>,
    /// Writable maintenance inhibit.
    pub out_of_service: InPoint<bool>,
    /// Effective field output.
    pub applied: OutPoint<bool>,
    /// Timed failure-to-open/failure-to-close diagnostic.
    pub fault: OutPoint<bool>,
}

/// Composes Boolean valve control, service inhibit and feedback alarm.
/// The permissive gates opening. Closing is always the safe command. This
/// variant observes a single open contact; it cannot prove a separate closed
/// limit, leakage, or mechanical isolation.
pub fn on_off_valve(
    plant: &mut PlantBuilder,
    c: &EquipmentConfig,
    feedback: InPoint<bool>,
    applied: OutPoint<bool>,
    permissive: InPoint<bool>,
    feedback_ticks: i64,
) -> Result<OnOffValve, crate::BuildError> {
    c.validate()?;
    let request = input(
        plant,
        c,
        0,
        (true, true),
        "request",
        "",
        "Requested opening; independent of actual feedback",
    );
    let oos = input(
        plant,
        c,
        1,
        (false, true),
        "oos",
        "",
        "Maintenance inhibit; closes the valve",
    );
    plant.journaled(oos);
    let healthy = plant.add(DigitalInputSpec::new(parameters([(
        "invert",
        Value::Bool(true),
    )])));
    let gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(0))]),
        3,
    ));
    let service_out = output(plant, c, 2, true, "in-service", "", "In-service state");
    let service_in = input(
        plant,
        c,
        3,
        (true, false),
        "service-copy",
        "",
        "Delivered service state",
    );
    plant.connect(oos, &healthy.input);
    plant.connect(&healthy.out, service_out);
    plant.connect(service_in, service_out);
    plant.connect(request, gate.input(1));
    let guard = plant.add(InterlockSpec::new(
        parameters([("safe_value", Value::Float(0.0))]),
        1,
    ));
    let anchor = input(
        plant,
        c,
        7,
        (0.0, false),
        "guard-anchor",
        "",
        "Held protection anchor",
    );
    let guard_out = output(
        plant,
        c,
        8,
        0.0,
        "guard-output",
        "",
        "Protection pass-through",
    );
    let tripped = output(
        plant,
        c,
        9,
        false,
        "tripped",
        "",
        "Opening blocked: unavailable permissive or maintenance inhibit",
    );
    let trip_copy = input(
        plant,
        c,
        10,
        (false, false),
        "trip-copy",
        "",
        "Delivered protection state",
    );
    let invert = plant.add(DigitalInputSpec::new(parameters([(
        "invert",
        Value::Bool(true),
    )])));
    let allowed = output(
        plant,
        c,
        11,
        false,
        "allowed",
        "",
        "Protection permits opening",
    );
    let allowed_copy = input(
        plant,
        c,
        12,
        (false, false),
        "allowed-copy",
        "",
        "Delivered protection permission",
    );
    plant.connect(anchor, &guard.input);
    plant.connect(permissive, &guard.permissive);
    plant.connect(oos, guard.trip(1));
    plant.connect(&guard.out, guard_out);
    plant.connect(&guard.tripped, tripped);
    plant.journaled(tripped);
    plant.connect(trip_copy, tripped);
    plant.connect(trip_copy, &invert.input);
    plant.connect(&invert.out, allowed);
    plant.connect(allowed_copy, allowed);
    plant.connect(allowed_copy, gate.input(2));
    plant.connect(service_in, gate.input(3));
    let gated = output(
        plant,
        c,
        4,
        false,
        "gated-request",
        "",
        "Opening demand after permissive and service inhibit",
    );
    let cmd = input(
        plant,
        c,
        5,
        (false, false),
        "command-copy",
        "",
        "Delivered effective demand",
    );
    plant.connect(&gate.out, gated);
    plant.connect(cmd, gated);
    let motor = plant.add(MotorSpec::new(parameters([(
        "fault_ticks",
        Value::Int(feedback_ticks),
    )])));
    let fault = output(
        plant,
        c,
        6,
        false,
        "fault",
        "",
        "Feedback disagreement beyond the engineered time budget",
    );
    plant.connect(cmd, &motor.cmd);
    plant.connect(feedback, &motor.run);
    plant.connect(&motor.out, applied);
    plant.connect(&motor.fault, fault);
    signal(
        plant,
        c,
        feedback.id(),
        "feedback",
        "",
        "Actual open feedback contact",
    );
    signal(
        plant,
        c,
        applied.id(),
        "applied",
        "",
        "Applied opening output",
    );
    let mut members = vec![healthy.id, guard.id, invert.id, gate.id, motor.id];
    let mut points = vec![
        feedback.id(),
        applied.id(),
        fault.id(),
        tripped.id(),
        permissive.id(),
        request.id(),
        oos.id(),
    ];
    fault_alarm(plant, c, fault, 20, &mut members, &mut points);
    plant.equipment(Equipment {
        id: c.id.clone(),
        label: c.label.clone(),
        kind: "on-off-valve".into(),
        components: members,
        points,
        controls: vec![
            control(
                request.id(),
                dcs_model::EquipmentControlRole::Request,
                "Opening request",
                Some(("Close", "Open")),
            ),
            control(
                oos.id(),
                dcs_model::EquipmentControlRole::OutOfService,
                "Service",
                Some(("In service", "Out of service")),
            ),
        ],
    });
    Ok(OnOffValve {
        request,
        out_of_service: oos,
        applied,
        fault,
    })
}

/// An analog actuator with protected manual/automatic selection.
#[derive(Debug, Clone)]
pub struct ModulatingValve {
    /// false = automatic; true = manual.
    pub mode: InPoint<bool>,
    /// Writable manual position request in the actuator's engineering unit.
    pub manual: InPoint<f64>,
    /// Selected request before protection.
    pub requested: OutPoint<f64>,
    /// Effective field command after protection.
    pub applied: OutPoint<f64>,
    /// Protection state; holds the safe closed output in either mode.
    pub tripped: OutPoint<bool>,
    /// Timed position discrepancy.
    pub fault: OutPoint<bool>,
}

/// Engineered transfer and feedback-diagnostic behavior, in percent and scans.
/// Dwell must cover the connected actuator's healthy travel and sensor lag.
#[derive(Debug, Clone, Copy)]
pub struct ModulatingValveConfig {
    /// Maximum change of selected demand per scan on transfer.
    pub transfer_delta: f64,
    /// Maximum absolute position error counted as agreement.
    pub tolerance: f64,
    /// Consecutive disagreeing scans before asserting the diagnostic.
    pub discrepancy_ticks: u64,
}

/// Composes analog actuation from existing station, protection and valve kinds.
/// `automatic` and feedback use percent opening. Untrusted demand or permissive
/// closes the valve through `interlock`. The caller engineers transfer and
/// diagnostic timing against its field dynamics.
pub fn modulating_valve(
    plant: &mut PlantBuilder,
    c: &EquipmentConfig,
    automatic: Source<f64>,
    feedback: InPoint<f64>,
    applied: OutPoint<f64>,
    permissive: InPoint<bool>,
    settings: ModulatingValveConfig,
) -> Result<ModulatingValve, crate::BuildError> {
    c.validate()?;
    if !settings.transfer_delta.is_finite()
        || settings.transfer_delta <= 0.0
        || !settings.tolerance.is_finite()
        || settings.tolerance < 0.0
        || settings.discrepancy_ticks > i64::MAX as u64
    {
        return Err(crate::BuildError::InvalidConfiguration {
            field: "modulating valve transfer/diagnostics".into(),
            reason: "positive finite transfer, nonnegative finite tolerance and representable dwell required".into(),
        });
    }
    let mode = input(
        plant,
        c,
        0,
        (false, true),
        "mode",
        "",
        "Manual selection; false automatic, true manual",
    );
    plant.journaled(mode);
    let manual = input(
        plant,
        c,
        1,
        (0.0, true),
        "manual",
        "%",
        "Manual opening request",
    );
    let station = plant.add(ManualStationSpec::new(parameters([(
        "transfer_delta",
        Value::Float(settings.transfer_delta),
    )])));
    plant.connect(automatic, &station.control);
    plant.connect(mode, &station.mode);
    plant.connect(manual, &station.manual);
    let requested = output(
        plant,
        c,
        2,
        0.0,
        "requested",
        "%",
        "Selected opening request before protection",
    );
    let selected = input(
        plant,
        c,
        3,
        (0.0, false),
        "selected-copy",
        "%",
        "Delivered selected request",
    );
    let active = output(
        plant,
        c,
        4,
        false,
        "manual-active",
        "",
        "Actual mode selection",
    );
    plant.connect(&station.out, requested);
    plant.connect(selected, requested);
    plant.connect(&station.manual_active, active);
    let guard = plant.add(InterlockSpec::new(
        parameters([("safe_value", Value::Float(0.0))]),
        0,
    ));
    plant.connect(selected, &guard.input);
    plant.connect(permissive, &guard.permissive);
    let guarded = output(plant, c, 5, 0.0, "guarded", "%", "Protected opening demand");
    let command = input(
        plant,
        c,
        6,
        (0.0, false),
        "command-copy",
        "%",
        "Delivered protected demand",
    );
    let tripped = output(
        plant,
        c,
        7,
        false,
        "tripped",
        "",
        "Opening blocked: permissive false or demand/measurement quality untrusted",
    );
    plant.connect(&guard.out, guarded);
    plant.connect(command, guarded);
    plant.connect(&guard.tripped, tripped);
    plant.journaled(tripped);
    let valve = plant.add(ValveSpec::new(parameters([
        ("tolerance", Value::Float(settings.tolerance)),
        (
            "discrepancy_ticks",
            Value::Int(settings.discrepancy_ticks as i64),
        ),
    ])));
    let fault = output(
        plant,
        c,
        8,
        false,
        "fault",
        "",
        "Actual position failed to follow the applied request",
    );
    plant.connect(command, &valve.cmd);
    plant.connect(feedback, &valve.fb);
    plant.connect(&valve.out, applied);
    plant.connect(&valve.discrepancy, fault);
    for (component, ports) in [
        (station.id, vec!["control", "manual", "out"]),
        (guard.id, vec!["in", "out"]),
        (valve.id, vec!["cmd", "fb", "out"]),
    ] {
        for port in ports {
            plant.port_unit(component, port, "%");
        }
    }
    signal(
        plant,
        c,
        feedback.id(),
        "feedback",
        "%",
        "Actual valve position feedback",
    );
    signal(
        plant,
        c,
        applied.id(),
        "applied",
        "%",
        "Applied position command after protection",
    );
    let mut members = vec![station.id, guard.id, valve.id];
    let mut points = vec![
        requested.id(),
        applied.id(),
        feedback.id(),
        tripped.id(),
        fault.id(),
        mode.id(),
        manual.id(),
    ];
    fault_alarm(plant, c, fault, 20, &mut members, &mut points);
    plant.equipment(Equipment {
        id: c.id.clone(),
        label: c.label.clone(),
        kind: "modulating-valve".into(),
        components: members,
        points,
        controls: vec![
            control(
                mode.id(),
                dcs_model::EquipmentControlRole::Mode,
                "Mode",
                Some(("Automatic", "Manual")),
            ),
            bounded_control(
                manual.id(),
                dcs_model::EquipmentControlRole::ManualOutput,
                "Manual opening",
                [0.0, 100.0],
            ),
        ],
    });
    Ok(ModulatingValve {
        mode,
        manual,
        requested,
        applied,
        tripped,
        fault,
    })
}

/// A scaled analog measurement with a managed low/high warning.
#[derive(Debug, Clone)]
pub struct Measurement {
    /// Scaled measured process value.
    pub value: OutPoint<f64>,
    /// Delivered value for any number of process consumers.
    pub delivered: InPoint<f64>,
    /// Alarm status; warning does not change process outputs.
    pub alarm: OutPoint<bool>,
}

/// Composes an engineering-unit measurement and its managed warning.
/// Raw values already use `unit`; identity scaling preserves quality. The
/// warning limits and hysteresis are independent from drawing display bounds.
pub fn measurement(
    plant: &mut PlantBuilder,
    c: &EquipmentConfig,
    raw: InPoint<f64>,
    unit: &str,
    bounds: [f64; 2],
    warning: [f64; 2],
) -> Result<Measurement, crate::BuildError> {
    c.validate()?;
    let scaling = plant.add(AnalogInputSpec::<f64>::new(parameters([
        ("raw_min", Value::Float(bounds[0])),
        ("raw_max", Value::Float(bounds[1])),
        ("eng_min", Value::Float(bounds[0])),
        ("eng_max", Value::Float(bounds[1])),
    ])));
    let value = output(plant, c, 0, bounds[0], "pv", unit, "Measured process value");
    let delivered = input(
        plant,
        c,
        1,
        (bounds[0], false),
        "pv-copy",
        unit,
        "Delivered process measurement",
    );
    plant.connect(raw, &scaling.raw);
    plant.connect(&scaling.out, value);
    plant.connect(delivered, value);
    plant.port_unit(scaling.id, "raw", unit);
    plant.port_unit(scaling.id, "out", unit);
    let warning_block = plant.add(ManagedLatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(warning[0])),
            ("high_limit", Value::Float(warning[1])),
            ("hysteresis", Value::Float((bounds[1] - bounds[0]) * 0.02)),
            ("priority", Value::Int(3)),
            ("class", Value::Int(2)),
            ("response_ticks", Value::Int(300)),
            ("max_shelve_ticks", Value::Int(300)),
        ]),
        ManagedInputs {
            shelve: true,
            ..ManagedInputs::default()
        },
        crate::Rationalization {
            consequence: "Measured process value is approaching its engineered operating boundary"
                .into(),
            required_action:
                "Check process equipment and measurement; restore the engineered operating range"
                    .into(),
            reference: c.id.clone(),
        },
    ));
    plant.connect(delivered, &warning_block.input);
    plant.port_unit(warning_block.id, "in", unit);
    for name in ["low_limit", "high_limit", "hysteresis"] {
        plant.param_unit(warning_block.id, name, unit);
    }
    let ack = input(
        plant,
        c,
        2,
        (false, true),
        "ack",
        "",
        "Acknowledge process warning",
    );
    let shelve = input(
        plant,
        c,
        3,
        (false, true),
        "shelve",
        "",
        "Shelve warning for at most 300 scans; reason required",
    );
    plant.requires_reason(shelve);
    plant.connect(ack, &warning_block.ack);
    plant.connect(shelve, warning_block.managed.shelve.as_ref().unwrap());
    let mut points = vec![value.id(), raw.id(), ack.id(), shelve.id()];
    let alarm = output(
        plant,
        c,
        4,
        false,
        "alarm",
        "",
        "Low or high process warning",
    );
    plant.connect(&warning_block.alarm, alarm);
    plant.journaled(alarm);
    points.push(alarm.id());
    for (offset, suffix, port) in [
        (5, "unacknowledged", &warning_block.unacknowledged),
        (6, "shelved", &warning_block.managed.shelved),
        (7, "suppressed", &warning_block.managed.suppressed),
        (8, "out-of-service", &warning_block.managed.out_of_service),
    ] {
        let point = output(
            plant,
            c,
            offset,
            false,
            suffix,
            "",
            "Managed process warning state",
        );
        plant.connect(port, point);
        plant.journaled(point);
        points.push(point.id());
    }
    plant.equipment(Equipment {
        id: c.id.clone(),
        label: c.label.clone(),
        kind: "analog-measurement".into(),
        components: vec![scaling.id, warning_block.id],
        points,
        controls: vec![],
    });
    Ok(Measurement {
        value,
        delivered,
        alarm,
    })
}

/// A parallel PID loop exposing setpoint, measurement and requested output.
#[derive(Debug, Clone)]
pub struct ControlLoop {
    /// Writable process setpoint in the measured engineering unit.
    pub setpoint: InPoint<f64>,
    /// Automatic opening demand, delivered for actuator composition.
    pub demand: InPoint<f64>,
    /// PID instance carrying checkpointed integral and measurement state.
    pub component: ComponentId,
}

/// Composes a level-to-opening PI controller using negative gains for draining.
/// The actuator owns manual selection and protection; PID output is explicitly
/// limited to 0–100 percent and uses conditional-integration anti-windup.
pub fn level_loop(
    plant: &mut PlantBuilder,
    c: &EquipmentConfig,
    pv: InPoint<f64>,
    setpoint: f64,
    dt: f64,
) -> Result<ControlLoop, crate::BuildError> {
    c.validate()?;
    let sp = input(
        plant,
        c,
        0,
        (setpoint, true),
        "setpoint",
        "m",
        "Level setpoint; independent of measured level",
    );
    let pid = plant.add(PidSpec::new(parameters([
        ("kp", Value::Float(-60.0)),
        ("ki", Value::Float(-0.4)),
        ("kd", Value::Float(0.0)),
        ("dt", Value::Float(dt)),
        ("out_min", Value::Float(0.0)),
        ("out_max", Value::Float(100.0)),
    ])));
    plant.connect(sp, &pid.sp);
    plant.connect(pv, &pid.pv);
    plant.port_unit(pid.id, "sp", "m");
    plant.port_unit(pid.id, "pv", "m");
    plant.port_unit(pid.id, "out", "%");
    let requested = output(
        plant,
        c,
        1,
        0.0,
        "requested",
        "%",
        "Automatic opening request before mode selection and protection",
    );
    let demand = input(
        plant,
        c,
        2,
        (0.0, false),
        "demand-copy",
        "%",
        "Delivered automatic opening request",
    );
    plant.connect(&pid.out, requested);
    plant.connect(demand, requested);
    plant.equipment(Equipment {
        id: c.id.clone(),
        label: c.label.clone(),
        kind: "control-loop".into(),
        components: vec![pid.id],
        points: vec![pv.id(), sp.id(), requested.id()],
        controls: vec![bounded_control(
            sp.id(),
            dcs_model::EquipmentControlRole::Setpoint,
            "Level setpoint",
            [0.8, 3.8],
        )],
    });
    Ok(ControlLoop {
        setpoint: sp,
        demand,
        component: pid.id,
    })
}
