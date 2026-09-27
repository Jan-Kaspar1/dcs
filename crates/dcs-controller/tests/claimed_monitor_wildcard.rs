//! The QA finding `field-claimed-monitor-undialable-under-wildcard-bind`:
//! the field-arbitrated rendezvous cannot fire when the field owner
//! binds the wildcard — the documented container deployment, where
//! every rig controller runs `--listen 0.0.0.0:<port>`. The owner
//! declares `monitor.local_addr()` on its write-ownership claim, and
//! the bound listener's address is the wildcard: a *bind* address,
//! meaningful to `bind(2)` and undialable as a target — every peer
//! that resolves it dials its own loopback instead of the successor.
//! On the defective build the plant server stored the declaration
//! verbatim, so after a failover won by a wildcard-bound peer every
//! demoted or orphaned sibling stranded: the fencing verdicts named a
//! monitor none of them could reach.
//!
//! The contract the finding demands: the claim's declared monitor is
//! stored dialable — a wildcard-declared IP resolves to the claiming
//! connection's proven source, the same substitute a `?peer=`
//! wildcard announce gets — so the field's arbitration names a
//! successor's tracking surface the superseded peers can actually
//! pull.
//!
//! The scripted reproduction runs the issue's own shape on the
//! unkeyed pair: one `dcs-plant-server`, a `--driven --remote` pair
//! (`a` launched active, `b` its standby) plus a third armed peer `c`
//! (`--standby a --auto-promote`), a foreign `claim_writer` preempting
//! `a`'s claim so `a` fenced-demotes and `c` self-promotes at its miss
//! budget — declaring its monitor on the conditional orphan grant.
//! Then the demoted `a` and the orphaned `b` must resolve the
//! claim-declared monitor and reconverge `tracking` inside a bounded
//! tick count, and the stored monitor must never be the undialable
//! wildcard. `c` runs twice: bound to a routable `127.0.0.1` listener
//! (the positive control, whose declaration is served verbatim) and
//! bound to `0.0.0.0` (the defect's shape, whose declaration the
//! server normalizes to the claim connection's source — here
//! loopback, where both resolve, so the assertion lands on the
//! *stored* address rather than the dial succeeding).

use dcs_core::{JournalEvent, Role, StandbySync};
use dcs_monitor::MonitorClient;
use dcs_sim_net::RemoteDriver;
use std::net::SocketAddr;
use std::path::Path;

mod support;

use support::{CONTROLLER, Spawned, listening_on, reachable, spawn_logged, spawn_plant};

/// The shared plant's model — the QA rig's own pump station.
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
/// The driven-tick bound on a standby converging on a live active.
const CONVERGE_BOUND: u64 = 24;
/// `c`'s armed failover budget — the orphan-miss count at which its
/// self-promotion takes the field.
const FAILOVER_BUDGET: &str = "4";
/// The driven ticks the stranded peers get to resolve the successor's
/// declared monitor and reconverge — resolution runs inside the
/// ordinary tracking cycle, so the bound is small.
const REJOIN_TICKS: u64 = 16;
/// The foreign attachment's claim token — the reproduction's rogue
/// `claim_writer{controller:false}` preemption.
const FOREIGN: u64 = 0xF0_21_61_6E;

/// A `--driven` controller *without* `--pair-token` — the unkeyed
/// shape the QA rig's default launch reproduces — listening on
/// `listen`, so the armed peer can bind the wildcard the container
/// deployment documents.
fn spawn_unkeyed(model: &str, extra: &[String], listen: &str, dt: &str) -> Spawned {
    let mut args = vec![model.to_string()];
    args.extend(extra.iter().cloned());
    for arg in ["--listen", listen, "--driven", "--dt", dt] {
        args.push(arg.to_string());
    }
    spawn_logged(Path::new(CONTROLLER), &args, listening_on).0
}

fn tracking(client: &MonitorClient) -> bool {
    matches!(
        client.role().unwrap().sync,
        Some(StandbySync::Tracking { .. })
    )
}

/// The issue's reproduction with `c` bound on `c_listen` — routable
/// `127.0.0.1:0` or wildcard `0.0.0.0:0`. Returns the monitor endpoint
/// the field's standing claim served to the peers it fenced, plus
/// `c`'s dialable address — equal under the fix, whether `c` declared
/// a routable listener or the wildcard the server normalized.
fn reproduction(c_listen: &str) -> (SocketAddr, SocketAddr) {
    let plant = spawn_plant(Path::new(STATION), Path::new(DYNAMICS));
    let remote = ["--remote".to_string(), plant.addr.to_string()];
    let a_process = spawn_unkeyed(STATION, &remote, "127.0.0.1:0", DT);
    let mut b_args = remote.to_vec();
    b_args.extend(["--standby".to_string(), a_process.addr.to_string()]);
    let b_process = spawn_unkeyed(STATION, &b_args, "127.0.0.1:0", DT);
    let mut c_args = remote.to_vec();
    c_args.extend([
        "--standby".to_string(),
        a_process.addr.to_string(),
        "--auto-promote".to_string(),
        FAILOVER_BUDGET.to_string(),
    ]);
    let c_process = spawn_unkeyed(STATION, &c_args, c_listen, DT);
    let a = MonitorClient::new(a_process.addr);
    let b = MonitorClient::new(b_process.addr);
    // `c`'s bound address may be the wildcard — clients dial its
    // reachable form, exactly as the deployment's peers do through
    // the container's routable IP.
    let c_addr = reachable(c_process.addr);
    let c = MonitorClient::new(c_addr);
    let field = RemoteDriver::connect(plant.addr).unwrap();

    // Converge both standbys on the launched active — the run's
    // healthy precondition.
    let mut converged = false;
    for _ in 0..CONVERGE_BOUND {
        b.advance(1).unwrap();
        c.advance(1).unwrap();
        a.advance(1).unwrap();
        if tracking(&b) && tracking(&c) {
            converged = true;
            break;
        }
    }
    assert!(converged, "the pair's standbys never converged on a");

    // The preemption: a foreign `claim_writer` under a tool's token
    // (`controller: false`, the reproduction's plant-socket shape)
    // takes the field's arbitration unconditionally. The superseded
    // owner's next field write is fenced and demotes it in place.
    field.claim_writer(FOREIGN).unwrap();
    a.advance(1).unwrap();
    assert!(
        matches!(a.role().unwrap().role, Role::Demoting | Role::Standby),
        "a fenced write must demote the superseded owner in place"
    );
    assert!(
        a.journal(0)
            .unwrap()
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::FieldClaimLost { .. })),
        "the fencing loss must journal on the superseded owner"
    );

    // The armed peer's failover: `a`'s demoted checkpoints report no
    // field owner, so `c`'s pulls count orphan misses until the
    // budget's scan boundary self-promotes it through the conditional
    // orphan grant — which declares `c`'s monitor on the claim.
    let mut promoted = false;
    for _ in 0..12 {
        c.advance(1).unwrap();
        a.advance(1).unwrap();
        if c.role().unwrap().role == Role::Active {
            promoted = true;
            break;
        }
    }
    assert!(
        promoted,
        "the armed peer never self-promoted at its miss budget"
    );

    // What the field's arbitration now names as the successor: every
    // claim-state verdict — a probe is one — carries the standing
    // claim's declared monitor. Under the defect this was the
    // wildcard bind address verbatim.
    assert_eq!(
        field.probe_writer().unwrap(),
        dcs_core::FieldClaim::Held,
        "the promoted peer must hold the field"
    );
    let stored = field.claimed_monitor().unwrap();
    assert!(
        !stored.ip().is_unspecified(),
        "the stored claim monitor must never be the undialable \
         wildcard {stored} — the defect's verbatim declaration"
    );
    assert_eq!(
        stored, c_addr,
        "a wildcard declaration must store the claim connection's \
         proven source — the same substitute a ?peer= wildcard gets"
    );

    // The defect's wedge, now closed: the demoted `a` resolves the
    // declared monitor through its claim probes and adopts it; the
    // orphaned `b` resolves it through the orphan probe's leading
    // candidate. Both reconverge `tracking` inside the bound — under
    // the defect they stranded `unsynchronized`/`orphaned` forever.
    let mut reconverged = false;
    for _ in 0..REJOIN_TICKS {
        a.advance(1).unwrap();
        b.advance(1).unwrap();
        c.advance(1).unwrap();
        if tracking(&a) && tracking(&b) {
            reconverged = true;
            break;
        }
    }
    assert!(
        reconverged,
        "the demoted and orphaned peers never re-joined tracking: \
         a={:?} b={:?}",
        a.role().unwrap().sync,
        b.role().unwrap().sync
    );
    assert!(
        a.journal(0).unwrap().iter().any(|entry| matches!(
            entry.event,
            JournalEvent::TrackingSourceAdopted { source } if source == c_addr
        )),
        "the demoted peer must journal the successor's adoption \
         naming the routable monitor {c_addr}"
    );

    (stored, c_addr)
}

/// The positive control: `c` bound to a routable listener declares a
/// dialable monitor, which the field serves verbatim — the
/// reproduction's converging shape the defect build already passed.
#[test]
fn a_routable_claimed_monitor_rendezvous_converges_the_stranded_peers() {
    reproduction("127.0.0.1:0");
}

/// The defect's shape: `c` bound to the wildcard — every documented
/// container launch — declares `0.0.0.0`, which the server must never
/// store verbatim or the peers it fences strand on their own
/// loopbacks. Under the fix the declaration normalizes to the claim
/// connection's proven source and the stranded peers reconverge.
#[test]
fn a_wildcard_claimed_monitor_is_normalized_to_the_claim_connections_source() {
    reproduction("0.0.0.0:0");
}
