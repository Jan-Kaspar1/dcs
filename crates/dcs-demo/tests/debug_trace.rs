use dcs_core::{PointId, Sample, Value};
use dcs_demo::station_kinds::{self, points};

fn f(snap: &dcs_core::TelemetrySnapshot, p: PointId) -> String {
    match snap.points.iter().find(|t| t.point == p).and_then(|t| t.sample.as_ref()) {
        Some(Sample { value: Value::Float(v), quality, .. }) => format!("{v:7.3}/{quality:?}"),
        Some(Sample { value: Value::Bool(v), quality, .. }) => format!("{v:7}/{quality:?}"),
        Some(Sample { value: Value::Int(v), quality, .. }) => format!("{v:7}/{quality:?}"),
        other => format!("{other:?}"),
    }
}

#[test]
fn trace() {
    let run = station_kinds::run_bus().unwrap();
    for (i, s) in run.snapshots.iter().enumerate() {
        let scan = i as u64 + 1;
        if (48..=66).contains(&scan) || scan <= 12 {
            println!(
                "scan {scan:3} level={} sel={} dem={} cmd0={} cmd1={} run0={} run1={}",
                f(s, points::LEVEL_PRIMARY),
                f(s, points::LEVEL_SELECTED),
                f(s, points::DEMAND),
                f(s, points::cmd(0)),
                f(s, points::cmd(1)),
                f(s, points::run(0)),
                f(s, points::run(1)),
            );
        }
    }
}
