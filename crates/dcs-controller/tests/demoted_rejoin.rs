//! The QA finding `demoted-peer-strands-unsynchronized` at the process
//! boundary its reproduction ran against: a healthy *unkeyed* redundant
//! pair — no `--pair-token`, so the announced-source contract's keyed
//! verification cannot authenticate a tracking peer's `?peer=` hint —
//! where `POST /promote` on the converged standby preempts the field
//! claim and the superseded active's first fenced write demotes it in
//! place. On the defective build the ex-active then reports
//! `standby`/`unsynchronized` forever: every recovery needs a verified
//! tracking source, and an unkeyed run could never prove any endpoint —
//! only a container restart or operator reconfiguration restored the
//! pair, while `/role` kept serving a plausible-looking `standby`.
//!
//! The contract the finding demands: after any loss of active
//! ownership the demoted peer must be able to re-join tracking so the
//! pair retains failover redundancy. The shipped shape is the
//! field-arbitrated successor: a controller declares its monitor
//! address on the field's write-ownership claim, the fencing verdicts
//! report the standing claim's declared monitor, and a demoted peer
//! whose tracking path finds no proven source pulls the endpoint the
//! field's own arbitration names as owner — an identity no announced
//! hint could ever carry, since only actually holding the claim puts
//! a monitor under it — verifies the served checkpoint is this line's
//! field-owning continuation, and adopts it into the journal.

use dcs_core::{JournalEvent, Role, StandbySync, SwitchError};
use dcs_monitor::MonitorClient;
use std::path::Path;

mod support;

use support::{CONTROLLER, Spawned, listening_on, spawn_logged, spawn_plant};

/// The shared plant's model — the QA rig's own pump station: the same
/// checked-in document the lane's plant server and both controllers
/// run.
const STATION: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/pump_station.json"
);
/// The plant-side physics the lane runs.
const DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../qa_lane/fixtures/pump_station_dynamics.json"
);
const DT: &str = "0.1";
/// The driven-tick bound on the standby reaching `tracking` — the
/// lane converges it in a handful.
const CONVERGE_BOUND: u64 = 24;
/// The driven ticks the demoted peer gets to re-join tracking once
/// the new owner's claim declares its monitor — the resolution runs
/// inside the ordinary tracking cycle, so the bound is small.
const REJOIN_TICKS: u64 = 8;

/// A `--driven` controller *without* `--pair-token` — the unkeyed pair
/// shape the finding's reproduction runs: `support::spawn_controller*`
/// helpers always inject the keyed token, so this spawns the binary
/// directly with the same argument layout minus it.
fn spawn_unkeyed(model: &str, extra: &[String], dt: &str) -> Spawned {
    let mut args = vec![model.to_string()];
    args.extend(extra.iter().cloned());
    for arg in ["--listen", "127.0.0.1:0", "--driven", "--dt", dt] {
        args.push(arg.to_string());
    }
    spawn_logged(Path::new(CONTROLLER), &args, listening_on).0
}

/// The reproduction's unkeyed pair: one plant server, a launched
/// active claiming the field, and a `--standby` peer converged onto
/// its checkpoints.
struct Pair {
    _plant: Spawned,
    _active_process: Spawned,
    _standby_process: Spawned,
    active: MonitorClient,
    standby: MonitorClient,
}

fn converged_unkeyed_pair() -> Pair {
    let plant = spawn_plant(Path::new(STATION), Path::new(DYNAMICS));
    let remote = ["--remote".to_string(), plant.addr.to_string()];
    let active_process = spawn_unkeyed(STATION, &remote, DT);
    let mut standby_args = remote.to_vec();
    standby_args.extend(["--standby".to_string(), active_process.addr.to_string()]);
    let standby_process = spawn_unkeyed(STATION, &standby_args, DT);
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

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
    assert!(converged, "the standby never converged");

    Pair {
        _plant: plant,
        _active_process: active_process,
        _standby_process: standby_process,
        active,
        standby,
    }
}

/// The issue's reproduction: `POST /promote` on the converged standby
/// of a healthy unkeyed pair. The promoted peer's claim declares its
/// monitor; the ex-active's first fenced write demotes it in place,
/// and its tracking path adopts the field-arbitrated successor — the
/// demoted peer reconverges `tracking` and stays promotable, so the
/// pair's failover redundancy survives the switchover.
#[test]
fn a_remote_promotion_lets_the_demoted_peer_rejoin_tracking() {
    let pair = converged_unkeyed_pair();

    // The evidence's second defect surface stays closed by design:
    // `POST /demote` on the unkeyed owner still needs a proven source
    // to demote onto, and an unverified `?peer=` hint can never be
    // one — the recovery the finding demands is the demoted peer's
    // re-join, not an unproven demotion.
    let (status, body) = pair.active.request("POST", "/demote", None).unwrap();
    assert_eq!(status, 409, "{body}");
    assert_eq!(
        serde_json::from_str::<SwitchError>(&body).unwrap(),
        SwitchError::NoTrackingSource
    );

    // The takeover: `POST /promote` on the standby — no `POST /demote`
    // boundary ever runs on the old owner, so the tracking-source
    // recovery is the involuntary path's. The ex-active's next field
    // write is fenced and demotes it in place; the fenced settle scan
    // completes the reported transition.
    let promoted = pair.standby.promote().unwrap();
    assert_eq!(promoted.role, Role::Promoting);
    pair.active.advance(1).unwrap();
    pair.standby.advance(1).unwrap();
    pair.active.advance(1).unwrap();
    let report = pair.active.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        pair.active
            .journal(0)
            .unwrap()
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::FieldClaimLost { .. })),
        "the fencing preemption must journal on the demoted peer"
    );

    // The defect's wedge, now closed: a demoted unkeyed peer stayed
    // `unsynchronized` forever — no configured source and no provable
    // hint. Under the fix the standing claim's declared monitor
    // resolves as the tracking source: the adoption journals and the
    // peer reconverges `tracking` within a few scans.
    let mut rejoined = false;
    for _ in 0..REJOIN_TICKS {
        pair.standby.advance(1).unwrap();
        pair.active.advance(1).unwrap();
        if matches!(
            pair.active.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ) {
            rejoined = true;
            break;
        }
    }
    assert!(
        rejoined,
        "the demoted peer must re-join tracking — the defect left it \
         unsynchronized forever: {:?}",
        pair.active.role().unwrap().sync
    );
    assert!(
        pair.active
            .journal(0)
            .unwrap()
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::TrackingSourceAdopted { .. })),
        "the successor's adoption must journal on the demoted peer"
    );

    // Redundancy restored means the demoted peer is promotable again:
    // promote it back and the second demoted peer re-joins the same
    // way — the claim it just took declares its monitor, so the now-
    // fenced peer resolves it identically. A `409 not_converged` here
    // is the finding's stranded-peer signature.
    pair.active.promote().unwrap();
    pair.standby.advance(1).unwrap();
    pair.active.advance(1).unwrap();
    pair.standby.advance(1).unwrap();
    assert_eq!(pair.active.role().unwrap().role, Role::Active);
    let mut rejoined = false;
    for _ in 0..REJOIN_TICKS {
        pair.active.advance(1).unwrap();
        pair.standby.advance(1).unwrap();
        if matches!(
            pair.standby.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ) {
            rejoined = true;
            break;
        }
    }
    assert!(
        rejoined,
        "the second demoted peer must re-join tracking the same way: {:?}",
        pair.standby.role().unwrap().sync
    );
}
