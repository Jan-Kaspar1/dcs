//! The contract fields the hand-rolled assembler dropped: a supplied
//! model declaring `writable` and `stale_after_ticks` produces the same
//! scan image `dcs-controller`'s assembly path does — the point map
//! carries both declarations, a writable `In` point takes a receipted
//! `Command::WriteValue`, and a channel-bound input whose field report
//! stops changing lands `Uncertain(Stale)` once its declared budget
//! passes. Both halves run through the pieces `dcs_demo::run` itself is
//! built from — `dcs_demo::assemble` plus `dcs_assembly::assemble` on the
//! standard registries — and the freshness half lands in `run`'s
//! returned snapshot.

use dcs_core::{Command, CommandOutcome, PointId, Quality, QualityReason, Value, ValueKind};
use dcs_model::PlantModel;

/// The supplied document: the tank level loop in
/// `fixtures/tank_level.json`'s shape plus one scripted field input —
/// `bearing` — declared `writable` with a two-scan freshness budget. The
/// `sim-scripted` device plays an empty script, so its report never
/// changes after the first read: exactly what the budget exists to
/// catch. The point is not part of the loop, so regulation still runs
/// the documented trace.
const DOCUMENT: &str = r#"{
  "version": 1,
  "devices": [
    {
      "id": 1,
      "kind": "sim-ai",
      "channels": {
        "lt101_raw": { "direction": "in", "value_type": "float" },
        "lic101_sp": { "direction": "in", "value_type": "float" }
      }
    },
    {
      "id": 2,
      "kind": "sim-ao",
      "channels": {
        "lv101_cmd": { "direction": "out", "value_type": "float" }
      }
    },
    {
      "id": 3,
      "kind": "sim-scripted",
      "parameters": { "script": {} },
      "channels": {
        "bearing": { "direction": "in", "value_type": "float" }
      }
    }
  ],
  "io_points": [
    {
      "id": 10,
      "direction": "in",
      "value_type": "float",
      "channel": { "device": 1, "name": "lt101_raw" }
    },
    {
      "id": 11,
      "direction": "in",
      "value_type": "float",
      "channel": { "device": 1, "name": "lic101_sp" }
    },
    {
      "id": 20,
      "direction": "out",
      "value_type": "float",
      "channel": { "device": 2, "name": "lv101_cmd" }
    },
    {
      "id": 30,
      "direction": "in",
      "value_type": "float",
      "channel": { "device": 3, "name": "bearing" },
      "writable": true,
      "stale_after_ticks": 2
    }
  ],
  "signals": [
    {
      "id": 100,
      "name": "tank-level-raw",
      "source": 10,
      "unit": "mA",
      "description": "LT-101 tank level transmitter raw output"
    },
    {
      "id": 101,
      "name": "level-setpoint",
      "source": 11,
      "unit": "%",
      "description": "LIC-101 tank level setpoint"
    },
    {
      "id": 102,
      "name": "valve-command",
      "source": 20,
      "unit": "mA",
      "description": "LV-101 inlet valve command"
    },
    {
      "id": 103,
      "name": "bearing-temperature",
      "source": 30,
      "unit": "degC",
      "description": "P-101 bearing temperature"
    }
  ],
  "components": [
    {
      "id": 1,
      "kind": "analog-input",
      "parameters": {
        "raw_min": { "float": 4.0 },
        "raw_max": { "float": 20.0 },
        "eng_min": { "float": 0.0 },
        "eng_max": { "float": 100.0 }
      },
      "ports": {
        "raw": { "direction": "in", "value_type": "float" },
        "out": { "direction": "out", "value_type": "float" }
      }
    },
    {
      "id": 2,
      "kind": "pid",
      "parameters": {
        "kp": { "float": 0.5 },
        "ki": { "float": 0.3 },
        "dt": { "float": 0.1 },
        "out_min": { "float": 4.0 },
        "out_max": { "float": 20.0 }
      },
      "ports": {
        "sp": { "direction": "in", "value_type": "float" },
        "pv": { "direction": "in", "value_type": "float" },
        "out": { "direction": "out", "value_type": "float" }
      }
    }
  ],
  "connections": [
    {
      "from": { "point": 10 },
      "to": { "port": { "component": 1, "name": "raw" } }
    },
    {
      "from": { "port": { "component": 1, "name": "out" } },
      "to": { "port": { "component": 2, "name": "pv" } }
    },
    {
      "from": { "point": 11 },
      "to": { "port": { "component": 2, "name": "sp" } }
    },
    {
      "from": { "port": { "component": 2, "name": "out" } },
      "to": { "point": 20 }
    }
  ]
}"#;

/// The declared `writable` + `stale_after_ticks` field input.
const BEARING: PointId = PointId(30);

/// The point's declared freshness budget, in scans.
const STALE_BUDGET: u64 = 2;

/// Scans per run — enough for the loop to settle at the setpoint.
const SCANS: u64 = 400;

/// How close the level must track the setpoint, in percent of span.
const TOLERANCE_PERCENT: f64 = 0.5;

#[test]
fn declared_writable_and_freshness_budget_reach_the_scan_image() {
    // The executor `dcs_demo::run` builds: `dcs_demo::assemble` resolves
    // the driver, `dcs_assembly::assemble` the point map and components.
    let model = PlantModel::load(DOCUMENT).unwrap();
    let assembly = dcs_demo::assemble(&model).unwrap();
    let mut executor =
        dcs_assembly::assemble(&model, &dcs_controller::registry(), &assembly.driver).unwrap();

    // Both declarations land in the assembled point map — the dropped
    // spec tuples carried neither.
    let spec = executor.point_map().get(BEARING).unwrap();
    assert!(spec.writable);
    assert_eq!(spec.stale_after_ticks, Some(STALE_BUDGET));

    // `writable` honored: the receipted command surface accepts the
    // write and applies it at the next scan boundary — a `NotWritable`
    // rejection is what the dropped flag produced before.
    let receipt = executor.submit_command(Command::WriteValue {
        point: BEARING,
        kind: ValueKind::Float,
        value: Value::Float(50.0),
    });
    assert!(
        matches!(receipt.outcome, CommandOutcome::Accepted { .. }),
        "{receipt:?}"
    );
    executor.scan();
    assert_eq!(
        executor.sample(BEARING).map(|sample| sample.value),
        Some(Value::Float(50.0))
    );

    // `stale_after_ticks` honored through `dcs_demo::run`: the frozen
    // scripted report lands `Uncertain(Stale)` once the declared budget
    // passes — while the loop around it still regulates at the
    // setpoint, the flag's presence changing nothing else.
    let run = dcs_demo::run(DOCUMENT, SCANS).unwrap();
    let bearing = run
        .snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == BEARING)
        .unwrap();
    assert_eq!(
        bearing.sample.unwrap().quality,
        Quality::Uncertain(QualityReason::Stale)
    );
    assert!(
        (run.levels.last().copied().unwrap() - dcs_demo::SETPOINT_PERCENT).abs()
            <= TOLERANCE_PERCENT,
        "level {:?} did not hold the setpoint",
        run.levels.last()
    );
}
