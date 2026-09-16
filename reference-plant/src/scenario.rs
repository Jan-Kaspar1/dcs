//! The scripted deterministic simulation `ci/simulate.py` drives.
//!
//! The scenario is *generated from the composed layout* rather than
//! checked in beside it, so its point ids can never drift from the
//! composition: [`scenario`] takes the [`StationLayout`] the emit path
//! produced and serializes the leg list the runner consumes. Each leg
//! optionally submits `POST /command` bodies (each with the receipt
//! outcome it expects), optionally issues raw `dcs-plant-server`
//! protocol requests — the unfenced `inject_fault`/`clear_fault` pair a
//! field-side fault scenario uses — advances a declared number of scans
//! through `POST /scan`, and asserts observable point values on the
//! returned snapshot.
//!
//! The scenario exercises the station contract end to end against
//! `model/dynamics.json`: the well filling under the declared inflow,
//! the duty pump staging, the lag pump joining, the never-shelvable
//! high-level alarm annunciating and refusing a shelve request with
//! `NotWritable`, the receipted ack clearing the latch, the shelvable
//! low-level alarm's request standing inside its bound, a failed
//! primary instrument failing the selected measurement over to the
//! backup and back, manual takeover steering a pump independent of the
//! group — and handing its duty designation to the standby — the
//! out-of-service declaration suppressing a faulted pump's alarm, and
//! the pumped-down all-stop rotating duty for the next cycle.

use crate::station::{points, StationLayout};
use serde_json::{json, Map, Value};

/// A `write_value` command body in the attributed
/// `{"command":…,"actor":…}` envelope `POST /command` accepts.
fn write(point: u64, value: bool) -> Value {
    json!({
        "command": {
            "write_value": {
                "point": point,
                "kind": "bool",
                "value": {"bool": value},
            }
        },
        "actor": "ci-scenario",
    })
}

/// A `dcs-plant-server` protocol request injecting a quality fault on
/// `point` — the field-side fault the scenario drives.
fn inject_bad(point: u64) -> Value {
    json!({
        "op": "inject_fault",
        "point": point,
        "fault": {"quality": {"bad": "device_fault"}},
    })
}

/// A `dcs-plant-server` protocol request clearing `point`'s fault.
fn clear(point: u64) -> Value {
    json!({"op": "clear_fault", "point": point})
}

/// The `expect` map's `{ "<point id>": <expectation> }` shape — an
/// expectation is `{"bool": …}`, `{"int": …}`, `{"float": …}` for an
/// exact match, or `{"min": …, "max": …}` for an inclusive float
/// range.
fn expect(pairs: &[(u64, Value)]) -> Value {
    Value::Object(
        pairs
            .iter()
            .map(|(point, expectation)| (point.to_string(), expectation.clone()))
            .collect::<Map<String, Value>>(),
    )
}

/// One leg: `commands` are submitted to the monitor and their receipt
/// outcomes asserted against `expect_receipts`; `plant` requests go to
/// the plant server; then `scans` scans advance and `expect` is
/// asserted on the snapshot.
fn leg(
    name: &str,
    commands: Vec<Value>,
    expect_receipts: Vec<Value>,
    plant: Vec<Value>,
    scans: u64,
    expect: Value,
) -> Value {
    json!({
        "name": name,
        "commands": commands,
        "expect_receipts": expect_receipts,
        "plant": plant,
        "scans": scans,
        "expect": expect,
    })
}

fn quiet(name: &str, scans: u64, expect: Value) -> Value {
    leg(name, vec![], vec![], vec![], scans, expect)
}

/// The scenario document: `dt` is the simulated seconds per scan the
/// driven controller runs with; `legs` is the ordered command/scan/
/// expectation list.
pub fn scenario(layout: &StationLayout) -> Value {
    let demand = layout.demand.0;
    let duty = layout.duty.0;
    let staged = layout.staged.0;
    let duty_call = layout.duty_call.0;
    let lag_call = layout.lag_call.0;
    let high_level = layout.high_level.0;
    let below_cutoff = layout.below_cutoff.0;
    let backup_active = layout.backup_active.0;
    let level_sel = layout.level_selected.0;
    let lah = layout.high_level_alarm;
    let lal = layout.low_level_alarm;
    let backup_alarm = layout.backup_active_alarm;
    let pump0 = layout.pumps[0];
    let pump1 = layout.pumps[1];
    json!({
        "dt": 1.0,
        "legs": [
            // A cold start holds the station stopped: no demand, no
            // staged pump, no commands, and no alarm standing — the
            // delivered level copies seed the healthy mid-band value
            // rather than tripping the low-level alarm on a phantom
            // zero. The group already designates pump 1 as duty holder:
            // `duty` is the designation, not a running flag.
            quiet("cold-start-idle", 2, expect(&[
                (demand, json!({"int": 0})),
                (duty, json!({"int": 1})),
                (staged, json!({"int": 0})),
                (pump0.cmd.0, json!({"bool": false})),
                (pump1.cmd.0, json!({"bool": false})),
                (lah.alarm.0, json!({"bool": false})),
                (lal.alarm.0, json!({"bool": false})),
                (lal.unacknowledged.0, json!({"bool": false})),
            ])),
            // The declared inflow fills the well past `start`: the
            // chain calls duty and the group stages pump 1.
            quiet("duty-pump-stages", 2, expect(&[
                (demand, json!({"int": 1})),
                (staged, json!({"int": 1})),
                (duty_call, json!({"bool": true})),
                (duty, json!({"int": 1})),
            ])),
            // The level keeps rising past `lag_start`: the chain calls
            // the lag pump and the group stages both.
            quiet("lag-pump-stages", 2, expect(&[
                (demand, json!({"int": 2})),
                (staged, json!({"int": 2})),
                (lag_call, json!({"bool": true})),
            ])),
            // The level crosses `high`: the chain's flag and the
            // never-shelvable managed alarm both stand, the ack latch
            // latched, the well-full contact about to cut the inflow.
            quiet("high-level-annunciates", 2, expect(&[
                (high_level, json!({"bool": true})),
                (lah.alarm.0, json!({"bool": true})),
                (lah.unacknowledged.0, json!({"bool": true})),
                (level_sel, json!({"min": 4.6})),
            ])),
            // The never-shelvable policy: `lah`'s declared shelve port
            // binds a read-only point, so the request is refused at
            // submission with the named rejection — it never reaches
            // the field.
            leg(
                "lah-shelve-request-refused",
                vec![write(lah.shelve.unwrap().0, true)],
                vec![json!("not_writable")],
                vec![],
                1,
                expect(&[
                    (lah.shelved.0, json!({"bool": false})),
                    (lah.unacknowledged.0, json!({"bool": true})),
                ]),
            ),
            // The receipted ack path: the write lands at the next scan
            // boundary and the latch clears; `alarm` keeps reporting
            // process truth while the level stands above the trip.
            leg(
                "ack-clears-lah-latch",
                vec![write(lah.ack.0, true)],
                vec![json!("accepted")],
                vec![],
                1,
                expect(&[
                    (lah.unacknowledged.0, json!({"bool": false})),
                ]),
            ),
            // `ack` is level-observed: the operator releases it so the
            // next trip latches fresh.
            leg(
                "lah-ack-released",
                vec![write(lah.ack.0, false)],
                vec![json!("accepted")],
                vec![],
                1,
                expect(&[
                    (lah.unacknowledged.0, json!({"bool": false})),
                ]),
            ),
            // The primary instrument fails mid-drain: the selector
            // serves the backup, `backup_active` and its alarm stand —
            // and the station keeps controlling on the backup's
            // truthful reading.
            leg(
                "primary-fault-fails-over",
                vec![],
                vec![],
                vec![inject_bad(points::LEVEL_PRIMARY.0)],
                2,
                expect(&[
                    (backup_active, json!({"bool": true})),
                    (backup_alarm.alarm.0, json!({"bool": true})),
                    (backup_alarm.unacknowledged.0, json!({"bool": true})),
                ]),
            ),
            // The primary recovers: the selector returns to it the same
            // scan its sample turns `Good` again; the alarm's standing
            // output follows the condition down while the
            // unacknowledged latch holds for the operator.
            leg(
                "primary-recovers",
                vec![],
                vec![],
                vec![clear(points::LEVEL_PRIMARY.0)],
                2,
                expect(&[
                    (backup_active, json!({"bool": false})),
                    (backup_alarm.alarm.0, json!({"bool": false})),
                    (backup_alarm.unacknowledged.0, json!({"bool": true})),
                ]),
            ),
            leg(
                "backup-alarm-acknowledged",
                vec![write(backup_alarm.ack.0, true)],
                vec![json!("accepted")],
                vec![],
                1,
                expect(&[
                    (backup_alarm.unacknowledged.0, json!({"bool": false})),
                ]),
            ),
            leg(
                "backup-ack-released",
                vec![write(backup_alarm.ack.0, false)],
                vec![json!("accepted")],
                vec![],
                1,
                expect(&[]),
            ),
            // The pumped-down all-stop: both commands have dropped, the
            // completed cycle rotated duty to pump 2, and the residual
            // draw pulled the well into the dry-run band — the chain's
            // flag and the shelvable low-level alarm both stand.
            quiet("well-drains-all-stop", 2, expect(&[
                (demand, json!({"int": 0})),
                (staged, json!({"int": 0})),
                (duty, json!({"int": 2})),
                (pump0.cmd.0, json!({"bool": false})),
                (pump1.cmd.0, json!({"bool": false})),
                (below_cutoff, json!({"bool": true})),
                (lal.alarm.0, json!({"bool": true})),
                (lal.unacknowledged.0, json!({"bool": true})),
            ])),
            // The shelvable nuisance case: the low-level alarm's shelve
            // request is a writable point — the command is accepted and
            // `shelved` asserts inside the declared bound while the
            // standing state keeps reporting underneath it.
            leg(
                "lal-shelve-request-stands",
                vec![write(lal.shelve.unwrap().0, true)],
                vec![json!("accepted")],
                vec![],
                1,
                expect(&[
                    (lal.shelved.0, json!({"bool": true})),
                    (lal.unacknowledged.0, json!({"bool": true})),
                ]),
            ),
            leg(
                "lal-unshelved",
                vec![write(lal.shelve.unwrap().0, false)],
                vec![json!("accepted")],
                vec![],
                1,
                expect(&[
                    (lal.shelved.0, json!({"bool": false})),
                ]),
            ),
            leg(
                "lal-acknowledged",
                vec![write(lal.ack.0, true)],
                vec![json!("accepted")],
                vec![],
                1,
                expect(&[
                    (lal.unacknowledged.0, json!({"bool": false})),
                ]),
            ),
            leg(
                "lal-ack-released",
                vec![write(lal.ack.0, false)],
                vec![json!("accepted")],
                vec![],
                1,
                expect(&[]),
            ),
            // Manual takeover of the duty holder: `mode` selects the
            // operator's `hand` request over the group's — pump 2 runs
            // on hand with no demand standing — and its manual state
            // drops it from the availability aggregation, so the group
            // hands the duty designation to pump 1.
            leg(
                "manual-takeover-hands-duty-over",
                vec![write(pump1.mode.0, true), write(pump1.hand.0, true)],
                vec![json!("accepted"), json!("accepted")],
                vec![],
                6,
                expect(&[
                    (pump1.mode.0, json!({"bool": true})),
                    (pump1.cmd.0, json!({"bool": true})),
                    (pump1.run.0, json!({"bool": true})),
                    (pump1.avail.0, json!({"bool": false})),
                    (duty, json!({"int": 1})),
                ]),
            ),
            // Out of service: the guard cuts the pump's command path —
            // the hand request no longer reaches the motor — and the
            // pump stays out of the group's roster.
            leg(
                "out-of-service-inhibits",
                vec![write(pump1.out_of_service.0, true)],
                vec![json!("accepted")],
                vec![],
                4,
                expect(&[
                    (pump1.cmd.0, json!({"bool": false})),
                    (pump1.run.0, json!({"bool": false})),
                    (pump1.avail.0, json!({"bool": false})),
                    (pump1.fault_alarm.out_of_service.0, json!({"bool": true})),
                ]),
            ),
            // The run feedback stops reporting while the pump is out of
            // service: the motor's command/feedback proof still raises
            // its fault — `alarm` reports process truth — but the
            // designed suppression wiring holds the annunciation latch
            // clear.
            leg(
                "fault-suppressed-while-oos",
                vec![],
                vec![],
                vec![inject_bad(pump1.run.0)],
                3,
                expect(&[
                    (pump1.fault.0, json!({"bool": true})),
                    (pump1.fault_alarm.alarm.0, json!({"bool": true})),
                    (pump1.fault_alarm.unacknowledged.0, json!({"bool": false})),
                    (pump1.fault_alarm.suppressed.0, json!({"bool": true})),
                    (pump1.fault_alarm.out_of_service.0, json!({"bool": true})),
                ]),
            ),
            // Back in service the suppression releases and the fault
            // that outlasted it arrives as a fresh unacknowledged
            // transition.
            leg(
                "return-to-service-reannunciates",
                vec![write(pump1.out_of_service.0, false)],
                vec![json!("accepted")],
                vec![],
                2,
                expect(&[
                    (pump1.fault_alarm.out_of_service.0, json!({"bool": false})),
                    (pump1.fault_alarm.suppressed.0, json!({"bool": false})),
                    (pump1.fault_alarm.unacknowledged.0, json!({"bool": true})),
                ]),
            ),
            // The field fault clears: the motor's fault output drops,
            // the standing alarm follows, and the latch still waits on
            // the operator's ack.
            leg(
                "fault-clears",
                vec![],
                vec![],
                vec![clear(pump1.run.0)],
                4,
                expect(&[
                    (pump1.fault.0, json!({"bool": false})),
                    (pump1.fault_alarm.alarm.0, json!({"bool": false})),
                    (pump1.fault_alarm.unacknowledged.0, json!({"bool": true})),
                ]),
            ),
            leg(
                "fault-alarm-acknowledged",
                vec![write(pump1.fault_alarm.ack.0, true)],
                vec![json!("accepted")],
                vec![],
                1,
                expect(&[
                    (pump1.fault_alarm.unacknowledged.0, json!({"bool": false})),
                ]),
            ),
            // Back to auto and the pump rejoins the group's roster —
            // pump 1 keeps the duty designation it was handed.
            leg(
                "restored-to-auto",
                vec![
                    write(pump1.fault_alarm.ack.0, false),
                    write(pump1.mode.0, false),
                    write(pump1.hand.0, false),
                ],
                vec![json!("accepted"), json!("accepted"), json!("accepted")],
                vec![],
                4,
                expect(&[
                    (pump1.mode.0, json!({"bool": false})),
                    (pump1.avail.0, json!({"bool": true})),
                    (duty, json!({"int": 1})),
                ]),
            ),
        ],
    })
}
