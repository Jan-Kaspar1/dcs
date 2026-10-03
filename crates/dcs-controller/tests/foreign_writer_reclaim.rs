//! The QA finding `unkeyed-claimant-preempts-keyed-pair-and-strands-it`:
//! a keyed redundant pair whose field is taken by a process outside its
//! line had no way back. The `--pair-token` authenticates checkpoint
//! pulls and announced-source adoption but never the field's claim
//! arbitration, so `claim_writer` stayed unauthenticated and
//! unconditional: an unkeyed third attachment — which converges on the
//! pair entirely through public, unkey-gated `/checkpoint` pulls — could
//! seize the field outright, fence the legitimate owner out, and leave
//! both keyed peers reporting `orphaned` while the conditional orphan
//! grant refused a live incumbent. `POST /promote` on either peer
//! answered `409 field_claim_failed`, so the only recoveries were
//! operator actions on the *usurper* (demote it, or kill it) — nothing
//! on the pair's own surface restored it while that process lived.
//!
//! The contract this finding demands: keying the pair protects its own
//! control plane. The pair's field is taken back through the pair's own
//! promotion gate, against the live foreign writer, once — and only
//! once — that writer has been *diagnosed* as unable to prove the line's
//! key. The diagnosis is the pair key's one job nothing else can do: the
//! field's own arbitration names the standing writer's monitor endpoint
//! (decision 101), the keyed monitoring surface pulls that endpoint
//! under a fresh `?prove=` nonce, and an endpoint inside the line proves
//! it while one outside it cannot, whatever it serves. The verdict is a
//! *narrower* served state — `usurped`, "the tracked line has no field
//! owner and the writer the field does name is not this line's" —
//! because `orphaned` alone cannot tell a genuinely ownerless field from
//! a field a foreign process holds, and the two want opposite answers
//! from the promotion gate.
//!
//! The scripted reproduction is the finding's own: one `dcs-plant-server`,
//! a keyed pair `h` (launched active) and `s` (`--standby h`, both under
//! `--pair-token`) plus an **unkeyed** third attachment `u`
//! (`--standby h`, no token), `u` promoting to take the field, `h`
//! fencing-demoting and both keyed peers latching an ownerless verdict
//! on the sibling. Then: `h`'s `/role` must report the narrower
//! `usurped` verdict with `field_claim: held` — the pair has a writer,
//! and that writer is not in the pair — `POST /promote` on `h` must
//! succeed against the live foreign writer, and the journal must name
//! the endpoint the claim was taken from. The second direction is
//! asserted too: once a keyed peer holds the field again, its sibling's
//! diagnosis *clears*, because the standing writer proves the line's
//! key, and that sibling's promote falls back to the conditional grant —
//! the fix must not turn every keyed peer into an unconditional
//! preemptor.

use dcs_core::{FieldClaim, JournalEvent, Role, RoleReport, StandbySync, Tick};
use dcs_monitor::MonitorClient;
use dcs_sim_net::RemoteDriver;
use std::net::SocketAddr;
use std::path::Path;

mod support;

use support::{CONTROLLER, Spawned, listening_on, spawn_controller, spawn_plant};

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
/// The driven ticks the fenced-out peers get to resolve a tracking
/// source and settle onto the narrower verdict — the diagnosis runs
/// inside the ordinary tracking cycle, so the bound is small.
const DIAGNOSE_BOUND: u64 = 16;
/// The driven ticks the recovered owner gets to settle `active`.
const SETTLE_BOUND: u64 = 8;

/// A `--driven` controller *without* `--pair-token` — the unkeyed shape
/// the finding's third attachment runs — listening on `listen`.
fn spawn_unkeyed(model: &str, extra: &[String], listen: &str, dt: &str) -> Spawned {
    let mut args = vec![model.to_string()];
    args.extend(extra.iter().cloned());
    for arg in ["--listen", listen, "--driven", "--dt", dt] {
        args.push(arg.to_string());
    }
    support::spawn_logged(Path::new(CONTROLLER), &args, listening_on).0
}

fn tracking(client: &MonitorClient) -> bool {
    matches!(
        client.role().unwrap().sync,
        Some(StandbySync::Tracking { .. })
    )
}

/// One keyed peer's monitor, kept beside the endpoint it serves so the
/// journal's endpoint assertions name a real address rather than
/// whatever the client happened to dial.
struct Peer {
    client: MonitorClient,
    addr: SocketAddr,
}

impl Peer {
    fn new(addr: SocketAddr) -> Self {
        Self {
            client: MonitorClient::new(addr),
            addr,
        }
    }
}

/// The finding's reproduction, staged and asserted up to the point the
/// defect bit: the unkeyed attachment holds the field, the keyed owner
/// is fenced out, and the keyed sibling tracks a non-owner.
struct Reproduction {
    /// The keyed owner, fenced out of its own field.
    owner: Peer,
    /// The keyed sibling, which owns nothing.
    sibling: Peer,
    /// The unkeyed attachment holding the field.
    usurper: Peer,
    /// A bare field attachment, for the arbitration's own answers.
    field: RemoteDriver,
    /// The spawned processes, kept alive for as long as the rig the
    /// assertions stage on: a `Spawned` kills its child on drop, so the
    /// rig has to be held — destructuring this struct with `..` would
    /// kill every controller the moment the bindings land.
    processes: Vec<Spawned>,
}

fn reproduce() -> Reproduction {
    let plant = spawn_plant(Path::new(STATION), Path::new(DYNAMICS));
    let remote = ["--remote".to_string(), plant.addr.to_string()];
    // The legitimate pair, both keyed — `spawn_controller` declares the
    // shared `--pair-token` for each.
    let owner = spawn_controller(Path::new(STATION), &remote, DT);
    let mut sibling_args = remote.to_vec();
    sibling_args.extend(["--standby".to_string(), owner.addr.to_string()]);
    let sibling = spawn_controller(Path::new(STATION), &sibling_args, DT);
    // The foreign claimant: the same pair wiring, no key. It converges
    // on the active through public `/checkpoint` pulls, which no token
    // gates, so it reaches the promotion gate exactly as the finding
    // reports.
    let mut usurper_args = remote.to_vec();
    usurper_args.extend(["--standby".to_string(), owner.addr.to_string()]);
    let usurper = spawn_unkeyed(STATION, &usurper_args, "127.0.0.1:0", DT);
    let field_addr = plant.addr;
    // The `Spawned` handles are collected before the `Peer` wrappers
    // shadow the names: each one kills its child on drop, so the rig
    // has to outlive the function that builds it.
    let processes = vec![plant, owner, sibling, usurper];
    let owner = Peer::new(processes[1].addr);
    let sibling = Peer::new(processes[2].addr);
    let usurper = Peer::new(processes[3].addr);
    let field = RemoteDriver::connect(field_addr).unwrap();

    // Converge both standbys on the launched active — the run's healthy
    // precondition.
    let mut converged = false;
    for _ in 0..CONVERGE_BOUND {
        sibling.client.advance(1).unwrap();
        usurper.client.advance(1).unwrap();
        owner.client.advance(1).unwrap();
        if tracking(&sibling.client) && tracking(&usurper.client) {
            converged = true;
            break;
        }
    }
    assert!(
        converged,
        "the pair's standbys never converged on the owner"
    );

    // The preemption: the unkeyed attachment's `POST /promote` runs the
    // unconditional claim — its convergence is a plain `tracking`, and
    // `claim_writer` is unauthenticated and unconditional — so it takes
    // the field from under the keyed owner.
    assert_eq!(usurper.client.promote().unwrap().role, Role::Promoting);
    let mut took_over = false;
    for _ in 0..SETTLE_BOUND {
        owner.client.advance(1).unwrap();
        usurper.client.advance(1).unwrap();
        if owner.client.role().unwrap().role == Role::Standby {
            took_over = true;
            break;
        }
    }
    assert!(
        took_over,
        "the fenced write must demote the superseded keyed owner in place"
    );
    assert!(
        owner
            .client
            .journal(0)
            .unwrap()
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::FieldClaimLost { .. })),
        "the fencing loss must journal on the superseded keyed owner"
    );

    // Let the demoted pair settle onto the narrower verdict: the
    // owner's tracking source resolves through the announced set onto
    // the keyed sibling, which owns nothing, and the diagnosis runs
    // beside it.
    let mut diagnosed = false;
    for _ in 0..DIAGNOSE_BOUND {
        owner.client.advance(1).unwrap();
        sibling.client.advance(1).unwrap();
        usurper.client.advance(1).unwrap();
        // Both keyed peers reach it: the finding's own report is that
        // both latched, and each reads the diagnosis from the field's
        // own arbitration independently.
        if matches!(
            owner.client.role().unwrap().sync,
            Some(StandbySync::Usurped { .. })
        ) && matches!(
            sibling.client.role().unwrap().sync,
            Some(StandbySync::Usurped { .. })
        ) {
            diagnosed = true;
            break;
        }
    }
    assert!(
        diagnosed,
        "the fenced-out keyed owner never reported the narrower usurped \\
         verdict — /role cannot tell 'tracking a non-owner' from true \\
         ownerlessness, and the pair has no reclaim path",
    );

    Reproduction {
        owner,
        sibling,
        usurper,
        field,
        processes,
    }
}

/// The finding's defect: a foreign unkeyed writer holds the field and
/// both keyed peers latch an ownerless verdict. Under the fix `/role`
/// surfaces the narrower `usurped` state — "tracking a non-owner" —
/// beside the field's own `held` arbitration answer, and the pair's own
/// `POST /promote` takes the field back from the live writer instead of
/// answering `409 field_claim_failed` and demanding an operator act on
/// the usurper.
#[test]
fn a_keyed_pair_reclaims_its_field_from_an_unkeyed_writer() {
    let Reproduction {
        owner,
        sibling,
        usurper,
        field,
        mut processes,
    } = reproduce();

    // The served surface: the tracked line has no owner and the field
    // does, so the two reports say together what neither says alone.
    let report = owner.client.role().unwrap();
    assert!(
        matches!(report.sync, Some(StandbySync::Usurped { .. })),
        "the demoted keyed owner must report the usurped verdict, got {:?}",
        report.sync
    );
    assert_eq!(
        report.field_claim,
        Some(FieldClaim::Held),
        "the field's own arbitration holds — that is what the narrower \\
         verdict names"
    );
    assert_eq!(report.role, Role::Standby);

    // The reclaim path on the pair's own surface: an operator promote
    // runs the unconditional claim against the live foreign writer.
    // Under the defect this answered `409 field_claim_failed` on both
    // peers, repeatedly, for as long as the usurper lived.
    assert_eq!(
        owner.client.promote().unwrap().role,
        Role::Promoting,
        "the demoted keyed owner must be able to take the field back"
    );
    let mut recovered = false;
    for _ in 0..SETTLE_BOUND {
        owner.client.advance(1).unwrap();
        usurper.client.advance(1).unwrap();
        if owner.client.role().unwrap().role == Role::Active {
            recovered = true;
            break;
        }
    }
    assert!(recovered, "the keyed owner never settled active again");
    assert_eq!(
        field.probe_writer().unwrap(),
        FieldClaim::Held,
        "the keyed owner must hold the field again"
    );

    // The audit: which process held the field across the episode is
    // only reconstructable from the two ends — this run's
    // `field_claim_lost` and the peer's `foreign_claim_preempted`
    // naming the endpoint the claim was taken from.
    assert!(
        owner
            .client
            .journal(0)
            .unwrap()
            .iter()
            .any(|entry| matches!(
                entry.event,
                JournalEvent::ForeignClaimPreempted { writer } if writer == usurper.addr
            )),
        "the reclaim must journal the endpoint it took the field from, \
         the usurper's monitor {}",
        usurper.addr
    );

    // The usurper is fenced out like any superseded writer, and the
    // pair's redundancy is back: the sibling tracks the recovered owner
    // again.
    let mut fenced_out = false;
    for _ in 0..SETTLE_BOUND {
        usurper.client.advance(1).unwrap();
        if usurper.client.role().unwrap().role != Role::Active {
            fenced_out = true;
            break;
        }
    }
    assert!(
        fenced_out,
        "the usurper must be fenced out by the reclaim, not left writing"
    );
    let mut reconverged = false;
    for _ in 0..CONVERGE_BOUND {
        sibling.client.advance(1).unwrap();
        owner.client.advance(1).unwrap();
        if tracking(&sibling.client) {
            reconverged = true;
            break;
        }
    }
    assert!(
        reconverged,
        "the keyed sibling must reconverge on the recovered owner, got {:?}",
        sibling.client.role().unwrap().sync
    );
    assert!(
        processes
            .iter_mut()
            .all(|process| process.child.try_wait().unwrap().is_none()),
        "every scripted process must still be serving when the rig ends — \
         a controller that exited would make the reads above vacuous"
    );
}

/// The other half of the safety argument, and the one that keeps the
/// reclaim narrow: the diagnosis is a reading about *this* writer, not
/// a latch on the pair. Once a keyed peer holds the field again, that
/// writer proves the line's key on every pull — so no keyed peer ever
/// reads it as usurping the field, and the narrower verdict can never
/// make one pair member an unconditional preemptor of another. The
/// sibling converges `tracking` on the recovered owner and reports the
/// ordinary verdicts from there.
#[test]
fn a_keyed_writer_never_reads_as_usurping_its_own_pair() {
    let Reproduction {
        owner,
        sibling,
        usurper,
        mut processes,
        ..
    } = reproduce();

    // The sibling sees the same episode: the tracked line has no owner
    // while the field's writer cannot prove the line's key.
    assert!(
        matches!(
            sibling.client.role().unwrap().sync,
            Some(StandbySync::Usurped { .. })
        ),
        "the keyed sibling must read the same foreign writer as outside \
         its line, got {:?}",
        sibling.client.role().unwrap().sync
    );

    assert_eq!(owner.client.promote().unwrap().role, Role::Promoting);
    let mut recovered = false;
    let mut reconverged = false;
    for _ in 0..CONVERGE_BOUND {
        owner.client.advance(1).unwrap();
        sibling.client.advance(1).unwrap();
        usurper.client.advance(1).unwrap();
        assert!(
            !matches!(
                sibling.client.role().unwrap().sync,
                Some(StandbySync::Usurped { .. })
            ),
            "a writer inside the line proves the key, so no keyed peer \
             may read it as usurping the field"
        );
        if owner.client.role().unwrap().role == Role::Active {
            recovered = true;
        }
        if recovered && tracking(&sibling.client) {
            reconverged = true;
            break;
        }
    }
    assert!(recovered, "the keyed owner must settle active again");
    assert!(
        reconverged,
        "the keyed sibling must reconverge on the recovered owner, got {:?}",
        sibling.client.role().unwrap().sync
    );
    assert!(
        processes
            .iter_mut()
            .all(|process| process.child.try_wait().unwrap().is_none()),
        "every scripted process must still be serving when the rig ends — \
         a controller that exited would make the reads above vacuous"
    );
}

/// The served `/role` payload is the pair view's whole input, so the
/// narrower verdict must survive serialization as its own state rather
/// than collapsing into `orphaned`: a consumer has to be able to tell
/// "the field has a writer that is not this pair's" from "the field has
/// no writer", because the documented remedy differs — reclaim from the
/// writer, versus promote onto an empty field.
#[test]
fn the_role_report_serves_the_narrower_verdict_as_its_own_state() {
    let served: RoleReport = serde_json::from_value(serde_json::json!({
        "role": "standby",
        "tick": 41,
        "sync": {"usurped": {"aligned": 39}},
        "field_claim": "held",
    }))
    .unwrap();
    assert_eq!(
        served.sync,
        Some(StandbySync::Usurped { aligned: Tick(39) })
    );
    assert_eq!(served.field_claim, Some(FieldClaim::Held));
    let json = serde_json::to_value(&served).unwrap();
    assert_eq!(
        json["sync"],
        serde_json::json!({"usurped": {"aligned": 39}})
    );
    assert!(json["sync"].get("orphaned").is_none());
}
