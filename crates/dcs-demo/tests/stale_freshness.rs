//! The WW-REP/WW-LCM enabler evidence over the reference station: the
//! frozen-field driven run asserts the published freshness contract the
//! durable-history non-interference work assumes.
//!
//! `src/station_kinds.rs` documents the leg: inside [`FIELD_FREEZE`] the
//! boundary holds the field's own work, so the served field keeps
//! reporting its last samples while the driven scans advance — the
//! tick-deterministic analogue of the QA lane's writer-stop freeze.
//! The `net-flow` point's declared `stale_after_ticks` budget is the
//! model's only freshness declaration, so the lagging report presents
//! `Uncertain(Stale)` at the declared lag while every unbudgeted
//! neighbor keeps serving `Good`; the resumed step's fresh report
//! restores `Good`. These tests pin that the served snapshots carry the
//! stale interval, the bounded point history retains it, the journal
//! attributes the stale and recovery transitions to their producing
//! ticks, identical runs produce identical snapshots and journals, and
//! the `sim-bus` transport observes the identical contract.

use dcs_core::{JournalEvent, PointId, Quality, QualityReason, Sample, Tick};
use dcs_demo::station_kinds::{self, FIELD_FREEZE, LOCAL_DOCUMENT, TOTAL_SCANS, points};
use dcs_model::PlantModel;

/// The point telemetry for `point` in `snapshot`.
fn point_sample(snapshot: &dcs_core::TelemetrySnapshot, point: PointId) -> &Option<Sample> {
    &snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .unwrap_or_else(|| panic!("the snapshot serves point {}", point.0))
        .sample
}

/// The quality `point` reports in `snapshot`.
fn quality_at(snapshot: &dcs_core::TelemetrySnapshot, point: PointId) -> Option<Quality> {
    point_sample(snapshot, point)
        .as_ref()
        .map(|sample| sample.quality)
}

/// The `net-flow` point's declared freshness budget, read from the
/// checked-in document — the lag the contract is pinned against, not a
/// test-side constant.
fn declared_budget() -> u64 {
    PlantModel::load(LOCAL_DOCUMENT)
        .expect("the primary document loads")
        .io_points
        .iter()
        .find(|point| point.id == points::NET_FLOW)
        .expect("the document declares net-flow")
        .stale_after_ticks
        .expect("net-flow declares a freshness budget")
}

/// The scans the frozen field leaves the budgeted point stale on:
/// the last step fed the report scan `FIELD_FREEZE.start()` reads, the
/// budget lets that many lagging scans pass, and scan
/// `FIELD_FREEZE.end() + 1` is the last read of the held report before
/// the resumed step refreshes it.
fn stale_scans() -> std::ops::RangeInclusive<u64> {
    (*FIELD_FREEZE.start() + declared_budget() + 1)..=(*FIELD_FREEZE.end() + 1)
}

#[test]
fn the_lagging_budgeted_point_serves_stale_while_unbudgeted_neighbors_stay_good() {
    let run = station_kinds::run_frozen_local().expect("the frozen run completes");
    assert_eq!(run.snapshots.len() as u64, TOTAL_SCANS);
    let stale = Quality::Uncertain(QualityReason::Stale);
    let at = |scan: u64| &run.snapshots[scan as usize - 1];
    let window = stale_scans();

    // The budgeted point: Good until the lag passes the declared
    // budget, Stale for exactly the frozen reads past it, Good again
    // on the first fresh report — the declared lag, nothing wider.
    for scan in 1..=TOTAL_SCANS {
        let expected = if window.contains(&scan) {
            stale
        } else {
            Quality::Good
        };
        assert_eq!(
            quality_at(at(scan), points::NET_FLOW),
            Some(expected),
            "net-flow at scan {scan} (stale window {window:?})"
        );
    }

    // The unbudgeted neighbors read the same frozen reports — their
    // stamps lag identically — but no freshness declaration applies:
    // through every frozen read, the stale window included, they stay
    // Good. (Later scenario legs quality-inject these same points —
    // `level-primary`'s failover `Bad` at scan 63 — so the claim is
    // the frozen window, not the whole run.)
    for unbudgeted in [points::LEVEL_PRIMARY, points::LEVEL_BACKUP, points::INFLOW] {
        for scan in *FIELD_FREEZE.start() + 1..=*FIELD_FREEZE.end() + 1 {
            assert_eq!(
                quality_at(at(scan), unbudgeted),
                Some(Quality::Good),
                "unbudgeted point {} at scan {scan}",
                unbudgeted.0
            );
        }
    }
}

#[test]
fn the_stale_window_is_retained_in_history_and_journaled_at_its_ticks() {
    let run = station_kinds::run_frozen_local().expect("the frozen run completes");
    let stale = Quality::Uncertain(QualityReason::Stale);
    let window = stale_scans();
    let recovery = *window.end() + 1;

    // The bounded point history retains the whole stale interval:
    // every frozen-read tick's sample stands Stale, the recovery
    // tick's stands Good, and the ring's sequence numbers are the
    // append-ordered positions the gap contract numbers against.
    let history = run
        .history
        .iter()
        .find(|history| history.point == points::NET_FLOW)
        .expect("the history serves net-flow");
    assert_eq!(
        history.samples.len() as u64,
        TOTAL_SCANS,
        "the ring retains every scan's sample"
    );
    for (index, entry) in history.samples.iter().enumerate() {
        let seq = index as u64 + 1;
        assert_eq!(entry.seq, seq, "history seqs are the append positions");
        assert_eq!(entry.sample.tick, Tick(seq), "history ticks are the scans'");
        let expected = if window.contains(&seq) {
            stale
        } else {
            Quality::Good
        };
        assert_eq!(
            entry.sample.quality, expected,
            "history sample at tick {seq}"
        );
    }

    // The journal attributes the transitions to their producing ticks:
    // the point's quality record is exactly the first observation, the
    // stale transition at the window's first tick, and the recovery at
    // the first fresh tick after it — the stale window's durable
    // attribution.
    let transitions: Vec<(Tick, Option<Quality>, Quality)> = run
        .journal
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::QualityChanged { point, from, to } if *point == points::NET_FLOW => {
                Some((entry.tick, *from, *to))
            }
            _ => None,
        })
        .collect();
    assert_eq!(
        transitions,
        vec![
            (Tick(1), None, Quality::Good),
            (Tick(*window.start()), Some(Quality::Good), stale),
            (Tick(recovery), Some(stale), Quality::Good),
        ],
        "net-flow's journaled quality transitions"
    );
}

#[test]
fn identical_frozen_runs_produce_identical_snapshots_and_journals() {
    let first = station_kinds::run_frozen_local().expect("the first run completes");
    let second = station_kinds::run_frozen_local().expect("the second run completes");
    // The whole published record is reproducible: per-scan snapshots,
    // the transition journal, the bounded point histories, and the
    // command receipts.
    assert_eq!(
        serde_json::to_value(&first.snapshots).unwrap(),
        serde_json::to_value(&second.snapshots).unwrap(),
        "the snapshots diverged between identical runs"
    );
    assert_eq!(
        serde_json::to_value(&first.journal).unwrap(),
        serde_json::to_value(&second.journal).unwrap(),
        "the journal diverged between identical runs"
    );
    assert_eq!(
        serde_json::to_value(&first.history).unwrap(),
        serde_json::to_value(&second.history).unwrap(),
        "the history diverged between identical runs"
    );
    assert_eq!(first.receipts, second.receipts);
}

#[test]
fn the_frozen_contract_is_identical_across_kinds() {
    // The transport changes nothing the control plane observed: the
    // bus overlay's frozen leg holds the same reports, so the stale
    // window, the journal, and the retained history match the local
    // run's — `io_health.driver` normalized as the legitimately
    // kind-specific transport report `station_kinds` documents.
    let local = station_kinds::run_frozen_local().expect("the frozen primary run completes");
    let bus = station_kinds::run_frozen_bus().expect("the frozen bus run completes");
    for (index, (local, bus)) in local.snapshots.iter().zip(&bus.snapshots).enumerate() {
        let mut local = local.clone();
        let mut bus = bus.clone();
        local.io_health.driver = None;
        bus.io_health.driver = None;
        assert_eq!(
            serde_json::to_value(&local).unwrap(),
            serde_json::to_value(&bus).unwrap(),
            "scan {} snapshots diverged across transports",
            index + 1
        );
    }
    assert_eq!(local.journal, bus.journal);
    assert_eq!(local.history, bus.history);
    assert_eq!(local.receipts, bus.receipts);
}
