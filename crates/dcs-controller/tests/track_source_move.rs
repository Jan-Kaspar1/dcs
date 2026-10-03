//! The QA finding `standby-track-source-stale-ip-pin` — the pinned
//! tracking source: `--standby`/`--peer`/driven-track resolved the
//! configured peer name once at startup, and every pull then dialed
//! that `TrackTarget::Addr` for the run's life. A peer restarted onto
//! a new address under the same name — the routine container recreate
//! a redundant pair exists for — kept answering while the pin dialed
//! the old address forever: the standby degraded without recovery,
//! and an armed peer counted the phantom misses to its failover
//! budget and preempted the still-live owner.
//!
//! The contract the fix restores — `TrackTarget`'s documented "a
//! member restarted mid-failover rejoins without reconfiguration": a
//! configured source stays the declared name and every pull resolves
//! it fresh, so an address move under the name is followed and
//! tracking reconverges instead of stranding on the pinned address.
//!
//! The reproduction needs the peer's name to resolve to a moved
//! address mid-run. Real DNS is not scripted, but `/etc/hosts` is
//! consulted on every lookup — so the standby runs in an
//! unprivileged user+mount namespace where the peer's name is bound
//! to a hosts file this test rewrites, and the peer moves across
//! loopback addresses the way the rig's IPAM hands out fresh ones.

use dcs_core::{JournalEvent, Role, StandbySync};
use dcs_monitor::MonitorClient;
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::{self, JoinHandle};

mod support;

use support::{
    CONTROLLER, PAIR_TOKEN, SimTcp, Spawned, controller_model, kill, listening_on,
    spawn_controller, spawn_logged, spawn_plant,
};

/// The shared plant's model and physics — the tank loop the failover
/// scenarios run, re-pointed at `sim-tcp` for the controller pair.
const PLANT_MODEL: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-plant/fixtures/tank_loop.json"
);
const PLANT_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-plant/fixtures/tank_loop_dynamics.json"
);
const MODEL_SOURCE: &str = include_str!("../../dcs-plant/fixtures/tank_loop.json");
const DT: &str = "0.1";
/// The failover budget the armed standby runs under — a move window
/// must reconverge inside it, or the phantom failover the finding
/// recorded fires against the live owner.
const BUDGET: u32 = 3;
/// The name the standby's configured source is declared under. Its
/// resolution is this test's lever: the hosts line bound into the
/// standby's private mount namespace.
const PEER_NAME: &str = "peer-a";
/// Driven ticks the standby gets to converge on the freshly resolved
/// name after the move — the re-resolution is immediate inside the
/// pull, so the bound only needs to cover the apply boundary plus
/// slack for the defect's budget to visibly fire past.
const REJOIN_BOUND: u64 = 8;
/// Driven ticks for the initial convergence.
const CONVERGE_BOUND: u64 = 24;

/// Whether `args` on `tool` runs at all — the capability probe for
/// each piece of the mount-namespace rig.
fn runnable(tool: &str, args: &[&str]) -> bool {
    Command::new(tool)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

/// The capability this reproduction needs that the environment may
/// lack, if any — the skip message's missing piece.
fn missing_capability() -> Option<String> {
    for (tool, args) in [
        ("unshare", ["--version"].as_slice()),
        ("sh", ["-c", "true"].as_slice()),
        ("mount", ["--version"].as_slice()),
    ] {
        if !runnable(tool, args) {
            return Some(format!("`{tool}` is not runnable"));
        }
    }
    if !runnable("unshare", &["-Urm", "--propagation", "private", "true"]) {
        return Some("unprivileged user+mount namespaces are refused".to_string());
    }
    // The peer's restart lands on a different loopback address — the
    // container-recreate IP move on one host.
    if TcpListener::bind("127.0.0.2:0").is_err() {
        return Some("the 127.0.0.0/8 loopback range is not bindable".to_string());
    }
    None
}

/// A controller process carrying its own `/etc/hosts`: inside an
/// unprivileged user+mount namespace, `hosts` is bound over the
/// system file before the exec, so the process's name lookups follow
/// this test's file — and rewriting the file mid-run moves the
/// declared peer name exactly like the rig's IPAM re-issuing the
/// container's address.
fn spawn_namespaced_controller(hosts: &Path, args: &[String]) -> Spawned {
    let script = format!(
        "mount --bind \"$1\" /etc/hosts && shift && exec {} \"$@\"",
        CONTROLLER
    );
    let mut argv = vec![
        "-Urm".to_string(),
        "--propagation".to_string(),
        "private".to_string(),
        "--".to_string(),
        "sh".to_string(),
        "-c".to_string(),
        script,
        "dcs-controller".to_string(),
        hosts.to_str().unwrap().to_string(),
    ];
    argv.extend(args.iter().cloned());
    spawn_logged(Path::new("unshare"), &argv, listening_on).0
}

/// The placeholder occupying the moved peer's old address — the rig's
/// IPAM filling the vacated container address with an unrelated
/// workload: it accepts and immediately drops every connection, so a
/// pull still pinned on the old address keeps failing even though the
/// address itself stays live and refuses nothing.
struct Placeholder {
    addr: SocketAddr,
    stop: Arc<AtomicBool>,
    accept: Option<JoinHandle<()>>,
}

impl Placeholder {
    /// Binds `addr` and drops every connection it accepts — the
    /// occupied-but-dead shape the vacated container address takes.
    fn holding(addr: SocketAddr) -> Self {
        // The just-killed peer's listener socket is free the moment the
        // process is reaped; the retry only covers the kernel's own
        // teardown lag.
        let listener = (0..10)
            .find_map(|_| {
                let bound = TcpListener::bind(addr).ok();
                if bound.is_none() {
                    thread::sleep(std::time::Duration::from_millis(100));
                }
                bound
            })
            .unwrap_or_else(|| panic!("cannot occupy {addr}"));
        let stop = Arc::new(AtomicBool::new(false));
        let accept = {
            let stop = Arc::clone(&stop);
            thread::spawn(move || {
                for stream in listener.incoming() {
                    if stop.load(Ordering::Relaxed) {
                        return;
                    }
                    drop(stream);
                }
            })
        };
        Self {
            addr,
            stop,
            accept: Some(accept),
        }
    }
}

impl Drop for Placeholder {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        let _ = TcpStream::connect(self.addr);
        if let Some(accept) = self.accept.take() {
            let _ = accept.join();
        }
    }
}

/// The finding's reproduction: an armed standby configured with the
/// peer's *name* watches the name's owner restart onto a new address
/// mid-run — the configured `host:port` must keep rendezvousing, so
/// the standby reconverges `tracking` on the moved endpoint inside a
/// bounded window and the armed failover never fires against the
/// still-live owner. On the pinned build the same leg strands the
/// standby on the old address, counts the phantom misses to its
/// budget, and demotes the restarted owner — all three assertions
/// fail.
#[test]
fn a_configured_name_follows_the_peers_restart_onto_a_new_address() {
    if let Some(missing) = missing_capability() {
        eprintln!("skipping the mount-namespace pair: {missing}");
        return;
    }

    let dir = std::env::temp_dir().join(format!("dcs-track-move-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let pair_plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let pair_model = controller_model(
        &dir,
        "pair.json",
        MODEL_SOURCE,
        pair_plant.addr,
        SimTcp::PerDevice,
    )
    .0;

    // The standby's private resolution source: `peer-a` resolves to
    // the launched active's loopback address — and the flip later
    // rewrites it to the restarted one's.
    let hosts = dir.join("hosts");
    let write_hosts = |ip: &str| {
        std::fs::write(
            &hosts,
            format!("{ip} {PEER_NAME}\n127.0.0.1 localhost\n::1 ip6-localhost ip6-loopback\n"),
        )
        .unwrap();
    };
    write_hosts("127.0.0.1");

    // The launched active at the address `peer-a` first resolves to,
    // and the armed standby configured with the name — never the
    // address — inside the mount namespace that resolves it.
    let mut active = spawn_controller(&pair_model, &[], DT);
    let a_port = active.addr.port();
    let standby_args = vec![
        pair_model.to_str().unwrap().to_string(),
        "--pair-token".to_string(),
        PAIR_TOKEN.to_string(),
        "--standby".to_string(),
        format!("{PEER_NAME}:{a_port}"),
        "--auto-promote".to_string(),
        BUDGET.to_string(),
        "--listen".to_string(),
        "127.0.0.1:0".to_string(),
        "--driven".to_string(),
        "--dt".to_string(),
        DT.to_string(),
    ];
    let standby_process = spawn_namespaced_controller(&hosts, &standby_args);
    let standby = MonitorClient::new(standby_process.addr);
    let active_client = MonitorClient::new(active.addr);

    let mut converged = false;
    for _ in 0..CONVERGE_BOUND {
        standby.advance(1).unwrap();
        active_client.advance(1).unwrap();
        if matches!(
            standby.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ) {
            converged = true;
            break;
        }
    }
    assert!(converged, "the standby never converged on the named peer");

    // The container recreate: the peer's process dies, its vacated
    // address is occupied by an unrelated workload, and the name's
    // owner comes back on a *new* address at the same service port —
    // the rig's IPAM move.
    kill(&mut active);
    let _placeholder = Placeholder::holding(SocketAddr::from(([127, 0, 0, 1], a_port)));
    let restart_args = vec![
        pair_model.to_str().unwrap().to_string(),
        "--pair-token".to_string(),
        PAIR_TOKEN.to_string(),
        "--listen".to_string(),
        format!("127.0.0.2:{a_port}"),
        "--driven".to_string(),
        "--dt".to_string(),
        DT.to_string(),
    ];
    let restarted = spawn_logged(Path::new(CONTROLLER), &restart_args, listening_on).0;
    let restarted_client = MonitorClient::new(restarted.addr);

    // Resolution catches up — the name now answers where the peer
    // actually lives.
    write_hosts("127.0.0.2");

    // The defect's window: well past the armed budget, so a stranded
    // pin fires the phantom failover this leg must not see. The
    // standby must reconverge on the moved name and stay a standby —
    // and the restarted owner must never be preempted.
    let mut rejoined = false;
    for _ in 0..REJOIN_BOUND {
        standby.advance(1).unwrap();
        restarted_client.advance(1).unwrap();
        let report = standby.role().unwrap();
        assert_eq!(
            report.role,
            Role::Standby,
            "the phantom failover fired: {report:?}"
        );
        if matches!(report.sync, Some(StandbySync::Tracking { .. })) {
            rejoined = true;
            break;
        }
    }
    assert!(
        rejoined,
        "the standby must reconverge on the moved name within {REJOIN_BOUND} scans: {:?}",
        standby.role().unwrap().sync
    );
    assert_eq!(
        restarted_client.role().unwrap().role,
        Role::Active,
        "the live owner must never be preempted by the stranded peer's failover"
    );
    assert!(
        restarted_client
            .journal(0)
            .unwrap()
            .iter()
            .all(|entry| !matches!(entry.event, JournalEvent::FieldClaimLost { .. })),
        "no claim preemption may journal on the still-live owner"
    );
    assert!(
        standby
            .journal(0)
            .unwrap()
            .iter()
            .all(|entry| !matches!(entry.event, JournalEvent::RoleChanged { .. })),
        "no role change may journal on the tracking standby"
    );

    let _ = std::fs::remove_dir_all(&dir);
}
