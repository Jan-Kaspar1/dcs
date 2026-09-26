//! The QA finding `unkeyed-fenced-demote-parks-unsynchronized` (#1045):
//! on the unkeyed pair — the QA rig's own default launch — a foreign
//! `claim_writer` fenced the field's owner mid-run, and the demoted
//! ex-owner reported `sync=unsynchronized` for the rest of the session
//! while its sibling served perfectly good field-owning checkpoints.
//! The announced-hint channel was no help: `?peer=` hints authenticate
//! only under the pair key — the standby-shaped-forgery findings made
//! adoption on an unkeyed run unprovable by design — so an unkeyed
//! demoted peer could never verify any endpoint it heard about, and
//! `promote` refused `not_converged` until a restart.
//!
//! The contract the finding demands: after losing the field claim
//! mid-run, the demoted peer resolves a tracking source and converges
//! — the redundant pair keeps its hot standby. The shipped shape:
//! every claim a controller asserts declares its bound monitor
//! endpoint with the field's write-ownership arbitration, and every
//! fencing verdict the arbitration hands down carries the standing
//! claim's declared monitor. The ex-owner the verdict fences out
//! therefore learns its successor's address from the field's own
//! arbitration — not an unauthenticated announce — and the monitor
//! still proves the candidate serves this line *as its field
//! owner* (same generation, bounded lead, `source_owns_field`, and
//! the keyed `line_proof` where a pair key stands) before a single
//! tracking pull follows it.
//!
//! The scripted reproduction runs the issue's own shape: one
//! `dcs-plant-server`, two `dcs-controller --driven --remote` peers
//! with no `--pair-token`, a foreign `claim_writer` fencing the owner,
//! the rogue claim released, the sibling promoted through the orphaned
//! window — and the ex-owner's served `sync` observed per scan as it
//! resolves the claim-declared monitor and converges `tracking`,
//! promotable again rather than parked `unsynchronized`.

use dcs_core::{IoDriver, IoError, JournalEvent, PointId, Role, StandbySync, Value};
use dcs_monitor::MonitorClient;
use dcs_sim_net::RemoteDriver;
use std::path::Path;

mod support;

use support::{CONTROLLER, Spawned, listening_on, spawn_logged, spawn_plant};

/// The shared plant's model — the QA rig's own pump station.
const STATION: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/pump_station.json"
);
/// The plant-side physics the lane runs — present so the driven scans
/// step the same field the QA reproduction fenced under.
const DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../qa_lane/fixtures/pump_station_dynamics.json"
);
const DT: &str = "0.1";
/// The driven-tick bound on the ex-owner's convergence — the resolve
/// and the applies it converges on complete inside the first scans
/// after the successor's claim stands; the bound only guards a hang.
const CONVERGE_BOUND: u64 = 12;

/// The foreign attachment's claim token — any token neither controller
/// generated stands in for the QA reproduction's rogue claim.
const FOREIGN: u64 = 0xF0_21_61_6E;

/// An unkeyed `dcs-controller` — the QA rig's default launch the
/// finding reproduced against. The shared harness helpers inject
/// `--pair-token` unless the caller declares one, so the unkeyed pair
/// spawns directly, exactly as `demote_replay.rs`'s unkeyed legs do.
fn spawn_unkeyed(model: &str, extra: &[String], dt: &str) -> (Spawned, Vec<String>) {
    let mut args = vec![model.to_string()];
    args.extend(extra.iter().cloned());
    for arg in ["--listen", "127.0.0.1:0", "--driven", "--dt", dt] {
        args.push(arg.to_string());
    }
    spawn_logged(Path::new(CONTROLLER), &args, listening_on)
}

/// The defect's reproduction, end to end on the unkeyed pair:
///
/// 1. The launched active claims the field; the `--standby` peer
///    converges `tracking` on it.
/// 2. A foreign `claim_writer` preempts the arbitration — the QA run's
///    `claim_writer(random)` — and the owner's first fenced write
///    demotes it in place. While the dead foreign token stands, the
///    ex-owner's reclaim probes refuse, and the carried monitor is
///    `None`: a monitor-less tool's claim declares no successor —
///    `unsynchronized` is the correct report for that window, since
///    the field names nothing to track.
/// 3. The rogue claim releases — the QA run's hold-then-release — and
///    the sibling's promote takes the orphaned field: its conditional
///    orphan grant claims under its token *with its monitor endpoint
///    declared*, so the standing claim now names a successor's
///    tracking surface.
/// 4. The ex-owner's next probes read the fencing verdict the
///    arbitration now answers with the successor's declared monitor;
///    the monitor verifies it — pulled checkpoint, same generation,
///    the field-owning stamp — and adopts it as the tracking source.
///    The served `sync` moves `unsynchronized` → `tracking` inside the
///    bound, and `promote` answers again: the pair's hot standby is
///    restored, matching the failover goal the finding cites.
#[test]
fn unkeyed_fenced_demote_converges_on_the_field_attested_successor() {
    let plant = spawn_plant(Path::new(STATION), Path::new(DYNAMICS));
    let remote = ["--remote".to_string(), plant.addr.to_string()];
    let (active_process, _preamble) = spawn_unkeyed(STATION, &remote, DT);
    let mut standby_args = remote.to_vec();
    standby_args.extend(["--standby".to_string(), active_process.addr.to_string()]);
    let (standby_process, _standby_preamble) = spawn_unkeyed(STATION, &standby_args, DT);
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);
    let field = RemoteDriver::connect(plant.addr).unwrap();

    // Converge the standby on the launched active — the pair's
    // healthy precondition.
    let mut converged = false;
    for _ in 0..CONVERGE_BOUND {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
        if matches!(
            standby.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ) {
            converged = true;
            break;
        }
    }
    assert!(converged, "the standby never converged on the active");

    // The foreign preemption: a `claim_writer` under a token neither
    // controller generated takes the field's write arbitration
    // unconditionally — declaring no monitor, exactly like the QA
    // run's plant-socket tool.
    field.claim_writer(FOREIGN).unwrap();

    // The superseded owner's next field write is fenced: the
    // documented `field_claim_lost` demotes it in place.
    active.advance(1).unwrap();
    assert!(
        matches!(active.role().unwrap().role, Role::Demoting | Role::Standby),
        "a fenced write must demote the superseded owner in place"
    );
    assert!(
        active
            .journal(0)
            .unwrap()
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::FieldClaimLost { .. })),
        "the fencing loss must journal on the superseded owner"
    );

    // While the dead foreign token stands the ex-owner's reclaim
    // probes refuse — and the verdicts name no monitor, because the
    // rogue claim declared none. `unsynchronized` is the
    // correct report for this window: the field names nothing to
    // track, so there is nothing to converge on.
    for tick in 1..=2 {
        active.advance(1).unwrap();
        let report = active.role().unwrap();
        assert_eq!(report.role, Role::Standby);
        assert!(
            matches!(report.sync, Some(StandbySync::Unsynchronized)),
            "tick {tick}: no field-named source exists while the \
             monitor-less foreign claim stands: {report:?}"
        );
    }

    // The rogue claim releases — the QA run's hold-then-release. The
    // demoted peer is not ticked in this window: with the field
    // unclaimed its reclaim probe would re-arm its own token and
    // re-take the field, closing the episode the other way. The
    // sibling pulls the ex-owner's ownerless checkpoints instead —
    // `source_owns_field: false` surfaces the line as `orphaned`,
    // promotable — and its promote takes the field through the
    // conditional orphan grant, declaring its own monitor endpoint
    // on the claim it raises.
    field.release_writer().unwrap();
    standby.advance(1).unwrap();
    let report = standby.role().unwrap();
    assert!(
        matches!(report.sync, Some(StandbySync::Orphaned { .. })),
        "the ownerless line must surface orphaned on the tracking peer: {report:?}"
    );
    let (status, body) = standby.request("POST", "/promote", None).unwrap();
    assert_eq!(
        status, 200,
        "the orphan promote must not be refused: {body}"
    );
    standby.advance(1).unwrap();
    assert_eq!(
        standby.role().unwrap().role,
        Role::Active,
        "the promoted peer must settle active on its field-owning scan"
    );

    // The defect's fix, observed on the served sync state over time:
    // every scan's claim probe now reads the fencing verdict naming
    // the successor's declared monitor, the monitor verifies it
    // serves this line as field owner, and the demoted peer
    // converges `tracking` — where the defect build reported
    // `unsynchronized` for the rest of the session.
    let mut converged = false;
    for tick in 1..=CONVERGE_BOUND {
        active.advance(1).unwrap();
        let report = active.role().unwrap();
        assert_eq!(report.role, Role::Standby, "tick {tick}: {report:?}");
        assert!(
            !matches!(report.sync, Some(StandbySync::Diverged { .. })),
            "tick {tick}: the demoted peer must never convict on the \
             stale staged image: {report:?}"
        );
        if matches!(report.sync, Some(StandbySync::Tracking { .. })) {
            converged = true;
            break;
        }
        assert!(
            matches!(report.sync, Some(StandbySync::Unsynchronized)),
            "tick {tick}: before the field-arbitrated resolve the only \
             honest report is unsynchronized: {report:?}"
        );
    }
    assert!(
        converged,
        "the demoted peer never resolved the successor the field named"
    );

    // Promotable again — the redundancy the finding's parked
    // `not_converged` refusal silently degraded to none. The promote's
    // unconditional claim preempts the sibling, whose own fenced
    // demotion then resolves this peer's declared monitor
    // the same way: the pair settles back into one active plus a
    // tracking hot standby.
    let (status, body) = active.request("POST", "/promote", None).unwrap();
    assert_eq!(
        status, 200,
        "a tracking peer must promote — the defect's not_converged \
         refusal is the wedge: {body}"
    );
    active.advance(1).unwrap();
    assert_eq!(active.role().unwrap().role, Role::Active);
    let mut reconverged = false;
    for _ in 0..CONVERGE_BOUND {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
        if matches!(
            standby.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ) {
            reconverged = true;
            break;
        }
    }
    assert!(
        reconverged,
        "the symmetric fencing loss must resolve this peer's endpoint \
         for the newly demoted sibling"
    );
    // And the foreign attachment's writes stay fenced under the
    // promoted claim — the failover closed on exactly one field owner.
    assert!(
        matches!(
            field.write(PointId(100), Value::Bool(true)),
            Err(IoError::Fenced(_))
        ),
        "the foreign claim must stay superseded by the promotion's own"
    );
}
