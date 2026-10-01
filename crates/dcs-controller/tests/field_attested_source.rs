//! The QA finding `unkeyed-fenced-demote-parks-unsynchronized` (#1045,
//! re-verified under #1167): on the unkeyed pair — the QA rig's own
//! default launch — a foreign
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
//!
//! The first verification of this fix caught the surviving half the
//! routable-bind leg could not see: the QA rig launches every
//! controller `--listen 0.0.0.0:<port>` — the documented container
//! deployment — so the successor's claim declared its wildcard *bind*
//! address, which the demoted peer dialed as its own loopback and
//! parked `unsynchronized` on anyway (the
//! `claimed-monitor-wildcard-undialable` finding, #1135/#1166). The
//! reproduction therefore runs twice: bound routable and bound to the
//! wildcard, where the field's arbitration must never store the
//! wildcard verbatim — the stored monitor normalizes to the claim
//! connection's proven source — and the demoted peer converges either
//! way. A third leg drives the unscripted resolution: ticked while
//! the field stands unclaimed, the demoted ex-owner's own reclaim
//! probe re-arms its token and re-takes the field — the other way a
//! released rogue claim closes, leaving the pair's hot standby
//! attached rather than parked.
//!
//! What stays intended behavior, answered for the record: while a
//! *monitor-less* claim stands — a tool's, or a foreign attachment's
//! — `unsynchronized` is the honest report, since the field names no
//! endpoint to track and the announced contract can prove nothing on
//! an unkeyed run. Permanent `unsynchronized` is intended only there
//! and while the standing claim's declared monitor cannot prove this
//! line's ownership; every other shape must converge.

use dcs_core::{IoDriver, IoError, JournalEvent, PointId, Role, StandbySync, Value};
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
/// finding reproduced against — bound on `listen`, so the run can
/// take the rig's own wildcard bind shape. The shared harness helpers
/// inject `--pair-token` unless the caller declares one, so the
/// unkeyed pair spawns directly, exactly as `demote_replay.rs`'s
/// unkeyed legs do.
fn spawn_unkeyed(model: &str, extra: &[String], listen: &str, dt: &str) -> Spawned {
    let mut args = vec![model.to_string()];
    args.extend(extra.iter().cloned());
    for arg in ["--listen", listen, "--driven", "--dt", dt] {
        args.push(arg.to_string());
    }
    spawn_logged(Path::new(CONTROLLER), &args, listening_on).0
}

/// The defect's reproduction, end to end on the unkeyed pair, with
/// both controllers bound on `listen` — routable `127.0.0.1:0`, or
/// the wildcard `0.0.0.0:0` every container launch the QA rig runs
/// binds. Returns the monitor endpoint the field's standing claim
/// declared once the successor owned it, plus the sibling's dialable
/// address — equal under the fix whether the sibling declared a
/// routable listener or the wildcard the server normalizes.
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
fn reproduction(listen: &str) -> (SocketAddr, SocketAddr) {
    let plant = spawn_plant(Path::new(STATION), Path::new(DYNAMICS));
    let remote = ["--remote".to_string(), plant.addr.to_string()];
    let active_process = spawn_unkeyed(STATION, &remote, listen, DT);
    let active_addr = reachable(active_process.addr);
    let mut standby_args = remote.to_vec();
    standby_args.extend(["--standby".to_string(), active_addr.to_string()]);
    let standby_process = spawn_unkeyed(STATION, &standby_args, listen, DT);
    let standby_addr = reachable(standby_process.addr);
    let active = MonitorClient::new(active_addr);
    let standby = MonitorClient::new(standby_addr);
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

    // What the field's arbitration now names as the successor: every
    // claim-state verdict carries the standing claim's declared
    // monitor. Under the verification-failure defect this was the
    // sibling's wildcard *bind* address verbatim — `0.0.0.0:<port>`,
    // which every peer dials as its own loopback — so the stored
    // monitor must be the claim connection's proven source instead,
    // the same substitute a `?peer=` wildcard announce resolves to.
    assert_eq!(
        field.probe_writer().unwrap(),
        dcs_core::FieldClaim::Held,
        "the promoted sibling must hold the field"
    );
    let stored = field.claimed_monitor().unwrap();
    assert!(
        !stored.ip().is_unspecified(),
        "the stored claim monitor must never be the undialable \
         wildcard {stored} — the verdict the defect's re-verification \
         parked on"
    );
    assert_eq!(
        stored, standby_addr,
        "the claim must declare the successor's dialable monitor \
         {standby_addr} — verbatim or normalized from the wildcard \
         bind, the rendezvous the demoted peer dials"
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
    assert!(
        active.journal(0).unwrap().iter().any(|entry| matches!(
            entry.event,
            JournalEvent::TrackingSourceAdopted { source } if source == standby_addr
        )),
        "the demoted peer must journal the successor's adoption \
         naming the routable monitor {standby_addr}"
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

    (stored, standby_addr)
}

/// The defect's reproduction on routable binds — the positive
/// control: every declaration is dialable verbatim, so the demoted
/// ex-owner resolves the claim-declared successor and the pair keeps
/// its hot standby.
#[test]
fn unkeyed_fenced_demote_converges_on_the_field_attested_successor() {
    reproduction("127.0.0.1:0");
}

/// The shape the fix's first verification actually parked on: both
/// controllers bound to the wildcard — the rig's and every documented
/// container launch's `--listen 0.0.0.0:<port>` — so the successor's
/// claim declares its bind address, which a verbatim-storing field
/// hands every fenced peer as its own loopback. Under the fix the
/// stored monitor is the claim connection's proven source — routable,
/// equal to the sibling's reachable address — and the demoted peer
/// adopts it: `unsynchronized` only ever reports the window where the
/// field named nothing to track, never the parked end-state the
/// defect left.
#[test]
fn unkeyed_fenced_demote_on_wildcard_binds_converges_on_the_normalized_successor() {
    let (stored, standby_addr) = reproduction("0.0.0.0:0");
    assert_eq!(
        stored, standby_addr,
        "a wildcard declaration must store the claim connection's \
         proven source — the normalization the verification-failure \
         verdict never applied"
    );
}

/// The unscripted resolution the driven promote leg suppresses on
/// purpose: the rogue claim released and the field standing
/// unclaimed, the demoted ex-owner's next scan probes its reclaim —
/// the bound conditional grant — and re-takes the field under its own
/// token, settling `active` again while the sibling holds `tracking`.
/// `unsynchronized` is a transient report here too — the parked
/// `standby`/`unsynchronized` the defect left has no resolution it
/// can survive: the released field is either re-armed by the ex-owner
/// or claimed by a successor whose declared monitor the demoted peer
/// adopts.
#[test]
fn unkeyed_fenced_demote_reclaims_the_released_field() {
    let plant = spawn_plant(Path::new(STATION), Path::new(DYNAMICS));
    let remote = ["--remote".to_string(), plant.addr.to_string()];
    let active_process = spawn_unkeyed(STATION, &remote, "0.0.0.0:0", DT);
    let active_addr = reachable(active_process.addr);
    let mut standby_args = remote.to_vec();
    standby_args.extend(["--standby".to_string(), active_addr.to_string()]);
    let standby_process = spawn_unkeyed(STATION, &standby_args, "0.0.0.0:0", DT);
    let standby_addr = reachable(standby_process.addr);
    let active = MonitorClient::new(active_addr);
    let standby = MonitorClient::new(standby_addr);
    let field = RemoteDriver::connect(plant.addr).unwrap();

    // The healthy precondition: the standby converges on the launched
    // active.
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

    // The reproduction's own fencing: the rogue claim preempts, the
    // owner demotes in place, the claim releases — and the demoted
    // peer is ticked straight through the unclaimed window the other
    // leg holds it out of.
    field.claim_writer(FOREIGN).unwrap();
    active.advance(1).unwrap();
    assert!(
        matches!(active.role().unwrap().role, Role::Demoting | Role::Standby),
        "a fenced write must demote the superseded owner in place"
    );
    field.release_writer().unwrap();

    // The released field re-arms under the ex-owner's token on its
    // next scans: the reclaim probe's bound conditional grant lands,
    // the gate re-opens `promoting`, and the field-owning scan settles
    // `active` — the resolved shape the QA run's parked
    // `standby`/`unsynchronized` never reached on the defect build.
    let mut reclaimed = false;
    for tick in 1..=CONVERGE_BOUND {
        active.advance(1).unwrap();
        let report = active.role().unwrap();
        assert!(
            matches!(report.role, Role::Standby | Role::Promoting | Role::Active),
            "tick {tick}: the demoted peer may only walk back toward \
             active: {report:?}"
        );
        if report.role == Role::Active {
            reclaimed = true;
            break;
        }
    }
    assert!(
        reclaimed,
        "the demoted peer's reclaim never re-armed its released claim"
    );
    assert!(
        matches!(field.probe_writer().unwrap(), dcs_core::FieldClaim::Held),
        "the reclaimed peer must hold the field again"
    );

    // The pair's redundancy out the other side: the sibling — never
    // fenced, never re-launched — keeps pulling the re-owned line and
    // settles back into `tracking`, the hot standby the finding's
    // parked peer could no longer be.
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
        "the sibling must keep tracking the re-owned line: {:?}",
        standby.role().unwrap().sync
    );
}
