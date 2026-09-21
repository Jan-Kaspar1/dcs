//! The independent-consumer proof (decision 79): the
//! `reference-plant/` tree in this repository is a verbatim-publishable
//! consumer repository. This test materializes it into a scratch
//! directory *outside* the workspace, rewrites only the dependency
//! remote to a `file://` stand-in for the published origin — the
//! recorded `rev` pin untouched, with the stand-in seeded to serve
//! exactly that commit — and runs the tree's own `ci/check.sh`
//! end to end: resolve, build, git-only lockfile sources,
//! byte-identical emit against the checked-in artifacts,
//! released-tooling acceptance — plus the contract's remaining
//! `dcs-model` surfaces: `schema` and `interface-schema` emissions
//! byte-pinned to the release record's artifacts fetched through the
//! stand-in remote at the pinned rev, `diff` legs over a doctored
//! compatible revision and the identical document, and
//! `summary`/`signal-index` recorded as run evidence — the manifest
//! fingerprint check, the
//! rig-definition consistency check asserting `deploy/compose.yaml`
//! instantiates `deploy/manifest.json`, the deterministic scripted
//! simulation, the served operator surface —
//! the signal index, page, snapshot descriptors, and journal asserted
//! against the emitted model's declaration, plus the `GET /schema`
//! block-interface registry's coverage of every declared component, a
//! kind-declared command's structured receipt through `POST /command`,
//! a kind-emitted event's arrival in the consumer-visible record, and
//! the served registry document's structural conformance to the
//! fetched record artifact — the `pair` stage, which runs the
//! manifest-declared standby pair on the released tooling: the second
//! controller converging to `tracking`, scans driven through
//! `POST /scan` keeping the peers identical, a receipted
//! `demote`/`promote` switching the roles, and the run continuing
//! bumplessly with the adopted receipts and the durable journal
//! files' transition records intact — the stage's negotiation leg
//! then launching a third released standby on a foreign-fingerprint
//! model document: the peer reporting the named non-converged
//! `degraded` state through `GET /role`, `POST /promote` answering
//! `409 not_converged`, the active undisturbed throughout, and a
//! control peer on the pair's own model converging and promoting
//! normally —
//! plus the pair contract's
//! refusal half: `POST /promote` before the standby's first transfer
//! answering the named `not_converged` refusal with no field hand-off,
//! a receipted write to the tracking standby answering the named
//! `not_active` rejection with no phantom effect or audit, and the
//! same promote succeeding once the standby tracks — plus the pair
//! contract's failure-handover leg: a proven duty-pump field-channel
//! fault through the plant protocol's declared `inject_fault` handing
//! `duty` to the standby pump inside the declared bound with the
//! faulted pump's `fault`/`avail` reporting the exclusion and the
//! managed fault alarm annunciating with journaled `point_changed`
//! evidence, the all-out `none_available`/`all_faulted` annunciation
//! on losing every pump, and the declared recovery with the pair's
//! controller roles unmoved — plus the pair
//! contract's takeover leg: with the pair tracking and the pump group
//! holding a duty demand, receipted `p101-mode`/`p101-hand`/`p101-oos`
//! writes through the active's `POST /command` producing the declared
//! manual leg — group-demand exclusion, the hand-driven run under the
//! thermal/moisture guards, a plant-protocol protection input's proven
//! fault and managed alarm, the out-of-service inhibit, and the
//! restore returning the pump to group control with each attributed
//! transition journaled in order — plus the pair contract's
//! staged-vs-field divergence leg: the tracking standby's checkpoint
//! pulls withheld through an observation window while a field-side
//! write lands through the run's plant-protocol client, the stale
//! peer's served `diverged` report naming the perturbed output, its
//! promote refused `not_converged` with no field hand-off, the
//! active's writes/receipts/journal undisturbed, and a write-free
//! control window reconverging and promoting normally — the `consumers`
//! stage, which replays that driven run under each consumer schedule
//! (no UI, polling, a stalled reader, churn, malformed/flooded
//! traffic, a UI
//! process restart) requiring identical digests, the `ctl` stage,
//! which exercises the released `dcs-ctl` operator CLI's receipted
//! `invoke` path, read subcommands, and named refusal modes against
//! the same driven run, the pair contract's report leg, which runs
//! the released `dcs-alarm-report` over the driven pair's served
//! journal and manifest-declared durable journal file — the declared
//! `AlarmReport` metric set asserted, the refusal modes exiting
//! nonzero — the pair contract's managed-lifecycle leg, which
//! exercises the emitted model's whole managed-alarm surface on the
//! deployed pair — the field-driven activation's journaled
//! `alarm`/`unacknowledged`, the receipted actor-attributed `ack`,
//! the bounded shelve's `shelved` report and auto-release at the
//! declared `max_shelve_ticks`, the never-shelvable shelve write's
//! named `not_writable` refusal, and the pump `oos` drive's declared
//! `out_of_service`/`suppressed` wiring through the suppressed trip
//! and the return to service, the durable journal's ordered record
//! audited and the pair's roles and driven inputs restored — the pair
//! contract's managed run-state carryover leg, which proves the
//! managed alarm kinds' checkpointed run state carries across a
//! takeover on the deployed pair — a per-pump fault alarm held
//! `out_of_service` through its wired `oos` point and tripped
//! suppressed, the shelvable alarm shelved mid-run through its
//! writable journaled `shelve` point, the documented
//! `demote`/`promote` landing inside the declared `max_shelve_ticks`
//! bound, the promoted peer asserting `shelved` stands and releases
//! at the tick the continued countdown expires — never a bound
//! restarted at the switch — `out_of_service` standing with
//! evaluation held, both durable journals' ordered records continuous
//! across the switch, and every driven input and the pair's launch
//! roles restored — the pair
//! contract's staging leg, which proves the deployed pair stages and
//! de-stages on level through the emitted model's declared setpoint
//! chain: both pumps held out of service through receipted `oos`
//! writes so the declared inflow raises the level unopposed, the
//! active's monitor asserting `demand` moves 0→1→2 only at the
//! declared `start`/`lag_start` crossings with `duty_call`/`lag_call`
//! reporting, the `high` crossing annunciating the managed high-level
//! alarm with journaled evidence, the releases staging the group with
//! the lag answering inside the declared `start_delay_ticks` and each
//! pump's `cmd`/`run` field outputs proving the start, and the staged
//! pumps drawing the level down through the declared de-stage order —
//! the lag's run releasing before the duty's — to the `below-cutoff`
//! floor, the durable journal audited for the ordered record and the
//! pair's roles unchanged — the pair contract's per-pump
//! out-of-service leg, which proves a receipted `oos` write on the
//! duty pump excludes it on the deployed pair — the in-service cone
//! and the aggregated availability dropping, `duty` handing to the
//! sibling inside the declared wiring bound, `staged` reporting the
//! available count, the held pump's command released through the
//! sibling's service — each managed per-pump alarm reporting the
//! `out_of_service`/`suppressed` states its declared lifecycle
//! bindings select, a mid-OOS run-contact fault asserting `alarm` as
//! process truth with the `unacknowledged` latch withheld, the false
//! write returning the pump to availability and re-annunciating the
//! outlasted trip, the receipted `ack` settling the latch, the next
//! cycle's rotation handing `duty` back, and the durable journal
//! carrying every managed transition beside the attributed
//! settlements — the pair contract's power-fail interlock leg, which
//! drives the station `power-fail` contact through the plant protocol
//! on the settled pair under a standing demand — `power-ok` and both
//! pumps' availability dropping, the motor commands releasing while
//! the chain's `demand` still stands, `none-available` and the managed
//! `power-fail` alarm annunciating with journaled evidence, the
//! receipted `power-fail-ack` clearing the latch mid-condition, and
//! the released contact re-staging the demand inside the declared
//! bounds with the pair's roles unchanged — and the `upgrade` stage,
//! which repins the materialized tree to the checkout's `HEAD`
//! (seeded into the stand-in beside the recorded rev) and re-runs the
//! full pipeline under the repin.
//!
//! Run alone from a clean checkout:
//!
//! ```sh
//! cargo test -p dcs-build --test reference_plant
//! ```
//!
//! Every failure the template's check can land is a named diagnostic
//! from `docs/release-contract.md` — this test surfaces them verbatim —
//! and the negative cases prove the new stage names the template
//! introduces: `stale-artifact`, `manifest-fingerprint-mismatch`,
//! `scenario-failed`, `rig-mismatch`, `schema-drift`,
//! `schema-mismatch`, `diff-mismatch`, `pair-failed`,
//! `negotiation-failed`, `refusal-failed`, `handover-failed`,
//! `takeover-failed`,
//! `peer-announce-failed`,
//! `divergence-missed`/`divergence-nondeterministic`,
//! `report-failed`/`report-nondeterministic`,
//! `managed-lifecycle-failed`/`managed-lifecycle-nondeterministic`,
//! `carry-failed`/`carry-nondeterministic`,
//! `staging-failed`/`staging-nondeterministic`,
//! `oos-failed`/`oos-nondeterministic`,
//! `power-trip-failed`/`power-trip-nondeterministic`,
//! and the `surface-mismatch` paths
//! a drifting interface registry, a receiptless declared command, or an
//! unobserved emitted event each produce.

mod common;

use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::Mutex;

use common::{CARGO, PIN_UNRESOLVABLE, head_rev, root};

/// The remote the published tree records — the string the materialized
/// copy's `Cargo.toml` rewrites to the `file://` stand-in.
const PUBLISHED_REMOTE: &str = "https://github.com/Jan-Kaspar1/dcs.git";

/// The workspace's build target directory, resolved through cargo so a
/// `CARGO_TARGET_DIR` override is honored.
fn target_dir() -> PathBuf {
    let output = Command::new(CARGO)
        .args(["metadata", "--format-version", "1", "--no-deps"])
        .current_dir(root())
        .output()
        .expect("cargo metadata runs");
    assert!(output.status.success(), "cargo metadata failed");
    let metadata: serde_json::Value =
        serde_json::from_slice(&output.stdout).expect("cargo metadata is JSON");
    PathBuf::from(metadata["target_directory"].as_str().unwrap())
}

/// Ensures the released tooling's local stand-ins — `dcs-model`,
/// `dcs-controller`, `dcs-plant-server`, `dcs-ctl`, and
/// `dcs-alarm-report` — are built for the check's `DCS_TOOLS`
/// substitution.
fn build_tools() -> PathBuf {
    let output = Command::new(CARGO)
        .args([
            "build",
            "--quiet",
            "-p",
            "dcs-model",
            "-p",
            "dcs-controller",
            "-p",
            "dcs-plant",
            "-p",
            "dcs-monitor",
        ])
        .current_dir(root())
        .output()
        .expect("cargo build of the released tooling runs");
    assert!(
        output.status.success(),
        "building the released tooling failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    target_dir().join("debug")
}

/// The `rev = "…"` pin the template's manifest records for the release
/// crates — the object the stand-in remote must serve.
fn pinned_rev(dir: &Path) -> String {
    let manifest = std::fs::read_to_string(dir.join("Cargo.toml")).unwrap();
    for line in manifest.lines() {
        if let Some(start) = line.find("rev = \"") {
            let rest = &line[start + "rev = \"".len()..];
            if let Some(end) = rest.find('"') {
                return rest[..end].to_owned();
            }
        }
    }
    panic!("the template's Cargo.toml records no rev pin");
}

/// Runs `git args` in `dir`, asserting success.
fn git(dir: &Path, args: &[&str]) {
    let output = Command::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .expect("git runs");
    assert!(
        output.status.success(),
        "git {} failed: {}",
        args.join(" "),
        String::from_utf8_lossy(&output.stderr)
    );
}

/// Ensures the checkout's object store contains `rev`. CI checkouts
/// are shallow (`actions/checkout` fetches at depth 1), so the
/// recorded release commit may be absent; fetch it from `origin` — the
/// published origin itself — when the store lacks it.
fn ensure_commit(rev: &str) {
    static FETCH_LOCK: Mutex<()> = Mutex::new(());
    let _guard = FETCH_LOCK.lock().unwrap();
    let present = Command::new("git")
        .args(["cat-file", "-e", &format!("{rev}^{{commit}}")])
        .current_dir(root())
        .output()
        .expect("git cat-file runs");
    if present.status.success() {
        return;
    }
    for remote in ["origin", PUBLISHED_REMOTE] {
        let fetch = Command::new("git")
            .args(["fetch", "--depth", "1", remote, rev])
            .current_dir(root())
            .output()
            .expect("git fetch runs");
        if fetch.status.success() {
            return;
        }
    }
    panic!("{PIN_UNRESOLVABLE}: no remote could serve the pinned rev {rev}");
}

/// A `file://` stand-in for the published origin: a bare repository in
/// the materialized scratch that serves exactly the recorded rev. The
/// workspace checkout alone cannot play the remote in CI — its shallow
/// object store lacks the pinned commit and serves no way to name it —
/// so the stand-in is seeded with that commit, the same object the
/// published origin serves for the recorded rev. The `upgrade` stage's
/// repin target — the checkout's `HEAD`, a later commit in the same
/// minor series — is seeded beside it so the repin resolves.
fn serve_pinned_rev(scratch: &Path) -> String {
    let rev = pinned_rev(scratch);
    ensure_commit(&rev);
    let remote = scratch.join("dcs-remote.git");
    git(scratch, &["init", "--bare", "dcs-remote.git"]);
    // The local transport serves the object directly; the published
    // origin is the fallback when the checkout cannot.
    let fetch = Command::new("git")
        .args(["fetch", "--depth", "1", &root().display().to_string(), &rev])
        .current_dir(&remote)
        .output()
        .expect("git fetch runs");
    if !fetch.status.success() {
        git(&remote, &["fetch", "--depth", "1", PUBLISHED_REMOTE, &rev]);
    }
    git(&remote, &["update-ref", "refs/heads/main", &rev]);
    let head = head_rev();
    git(
        &remote,
        &[
            "fetch",
            "--depth",
            "1",
            &root().display().to_string(),
            &head,
        ],
    );
    git(&remote, &["update-ref", "refs/heads/upgrade", &head]);
    format!("file://{}", remote.display())
}

/// Copies `src` into `dst` recursively.
fn copy_tree(src: &Path, dst: &Path) {
    std::fs::create_dir_all(dst).unwrap();
    for entry in std::fs::read_dir(src).unwrap() {
        let entry = entry.unwrap();
        let target = dst.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            copy_tree(&entry.path(), &target);
        } else {
            std::fs::copy(entry.path(), &target).unwrap();
        }
    }
}

/// A materialized copy of `reference-plant/` outside the workspace,
/// removed on drop.
struct Materialized {
    dir: PathBuf,
    remote: String,
}

impl Materialized {
    /// Copies the tree and rewrites only the dependency remote to the
    /// `file://` stand-in — the `rev` pin stays exactly as recorded.
    fn new() -> Self {
        let dir = std::env::temp_dir().join(format!(
            "dcs-reference-plant-{}-{:?}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        copy_tree(&root().join("reference-plant"), &dir);
        let remote = serve_pinned_rev(&dir);
        let manifest = dir.join("Cargo.toml");
        let source = std::fs::read_to_string(&manifest).unwrap();
        assert!(
            source.contains(PUBLISHED_REMOTE),
            "the template no longer pins the published remote"
        );
        std::fs::write(&manifest, source.replace(PUBLISHED_REMOTE, &remote)).unwrap();
        Self { dir, remote }
    }

    /// Runs the template's own clean-CI path against the `file://`
    /// stand-in remote and the locally built tooling — the same
    /// substitutions `consumer_release.rs` makes, plus the upgrade
    /// stage's repin target: the checkout's `HEAD`, a later commit in
    /// the same minor series the stand-in remote also serves.
    fn check(&self, tools: &Path) -> Output {
        Command::new("bash")
            .arg("ci/check.sh")
            .current_dir(&self.dir)
            .env("DCS_REMOTE", &self.remote)
            .env("DCS_TOOLS", tools)
            .env("DCS_UPGRADE_REV", head_rev())
            .env("CARGO_TARGET_DIR", self.dir.join("target"))
            .output()
            .expect("ci/check.sh runs")
    }
}

impl Drop for Materialized {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// The full materialization proof: the template's own clean-CI check
/// passes green outside the workspace — git-only lockfile sources,
/// byte-stable emit matching the checked-in artifacts, released-tooling
/// acceptance, the manifest fingerprint check, and the deterministic
/// scripted simulation.
#[test]
fn the_template_passes_its_own_clean_ci_outside_the_workspace() {
    let tools = build_tools();
    let copy = Materialized::new();
    let output = copy.check(&tools);
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        output.status.success(),
        "the template's ci/check.sh failed:\nstdout:\n{stdout}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    // The surface stage ran and held: the served block-interface
    // registry covered every declared component, the kind-declared
    // commands answered structured receipts, and the kind-emitted event
    // reached the consumer-visible record. The digest line reports the
    // counts — each must be nonzero for the proof to mean anything.
    let surface_line = stdout
        .lines()
        .find(|line| line.starts_with("surface-digest"))
        .unwrap_or_else(|| panic!("the surface stage reported no digest:\n{stdout}"));
    for phrase in ["declared commands receipted", "emitted events"] {
        let index = surface_line.find(phrase).unwrap_or_else(|| {
            panic!("the surface digest names no '{phrase}' count: {surface_line}")
        });
        let count: usize = surface_line[..index]
            .split_whitespace()
            .next_back()
            .and_then(|token| token.parse().ok())
            .unwrap_or_else(|| panic!("the '{phrase}' count is not a number: {surface_line}"));
        assert!(
            count > 0,
            "the surface stage proved no {phrase}: {surface_line}"
        );
    }
    // The extended tooling and surface legs ran and held: the recorded
    // schema artifacts were fetched and emitted byte-identically, the
    // diff legs named the doctored revision's change and none on the
    // identical document, summary and signal-index landed in the run's
    // evidence, the served registry document conformed to the record's
    // declared structure, and each leg's own doctored case reported its
    // named diagnostic.
    for line in [
        "emit the v0.2.0 record's artifacts byte-identically",
        "a drifted record artifact refused: schema-drift",
        "diff over the doctored compatible revision",
        "changed signal 10010",
        "diff over the identical document",
        "no changes",
        "failed diff expectations refused: diff-mismatch",
        "dcs-model summary (sha256",
        "dcs-model signal-index (sha256",
        "conforms to the recorded schema artifact",
        "missing-required refused: schema-mismatch",
        "mistyped-required refused: schema-mismatch",
    ] {
        assert!(
            stdout.contains(line),
            "the check transcript lacks '{line}':\n{stdout}"
        );
    }
    assert!(
        stdout.contains("== restart =="),
        "the restart stage did not run:\n{stdout}"
    );
    assert!(
        stdout.contains("restart-digest"),
        "the restart leg reported no digest:\n{stdout}"
    );
    assert!(
        stdout.contains("missing-state-file: reported, restart-resume-failed")
            && stdout.contains("corrupt-state-file: reported, restart-resume-failed"),
        "the restart leg's doctored cases did not report their named diagnostics:\n{stdout}"
    );
    // The pair stage ran and held: the manifest-declared standby
    // converged to tracking, the receipted demote/promote switched the
    // roles, the run continued bumplessly, and each peer's durable
    // journal file carried the transition records — its digest line
    // reports the evidence, and the broken-peer-flag case reported its
    // named diagnostic.
    assert!(
        stdout.contains("== pair =="),
        "the pair stage did not run:\n{stdout}"
    );
    let pair_line = stdout
        .lines()
        .find(|line| line.contains("pair-digest"))
        .unwrap_or_else(|| panic!("the pair leg reported no digest:\n{stdout}"));
    for phrase in ["switched at tick", "persisted journal records"] {
        assert!(
            pair_line.contains(phrase),
            "the pair digest names no '{phrase}' evidence: {pair_line}"
        );
    }
    assert!(
        stdout.contains("broken-peer-flag: reported, pair-failed"),
        "the pair leg's doctored case did not report its named diagnostic:\n{stdout}"
    );
    // The negotiation leg ran and held: the foreign-model standby
    // reported the named degraded negotiation state naming both
    // fingerprints, its promote drew the named refusal, the control
    // peer on the pair's own model settled active, and the
    // expect-tracking case reported the degraded state it saw.
    let negotiation_line = stdout
        .lines()
        .find(|line| line.contains("negotiation-digest"))
        .unwrap_or_else(|| panic!("the negotiation leg reported no digest:\n{stdout}"));
    for phrase in [
        "degraded",
        "promote refused not_converged",
        "settled active",
    ] {
        assert!(
            negotiation_line.contains(phrase),
            "the negotiation digest names no '{phrase}' evidence: {negotiation_line}"
        );
    }
    assert!(
        stdout.contains("expect-tracking: reported, negotiation-failed"),
        "the negotiation leg's doctored case did not report its named diagnostic:\n{stdout}"
    );
    // The pair contract's refusal half ran and held: the pre-transfer
    // promote answered not_converged, the standby-directed write
    // answered not_active, and the same promote succeeded once
    // tracking — its digest line reports the evidence, and the
    // doctored applied-expectation case reported its named diagnostic.
    let refusal_line = stdout
        .lines()
        .find(|line| line.contains("refusal-digest"))
        .unwrap_or_else(|| panic!("the refusal leg reported no digest:\n{stdout}"));
    for phrase in ["not_converged", "not_active", "promoted at tick"] {
        assert!(
            refusal_line.contains(phrase),
            "the refusal digest names no '{phrase}' evidence: {refusal_line}"
        );
    }
    assert!(
        stdout.contains("expect-applied: reported, refusal-failed"),
        "the refusal leg's doctored case did not report its named diagnostic:\n{stdout}"
    );
    // The pair contract's failure-handover leg ran and held: the
    // proven duty-pump channel fault handed duty to the standby pump
    // inside the declared bound, the all-out conditions annunciated on
    // losing every pump, and the restored inputs produced the declared
    // recovery — its digest line reports the evidence, and both
    // doctored cases reported their named diagnostic.
    let handover_line = stdout
        .lines()
        .find(|line| line.contains("handover-digest"))
        .unwrap_or_else(|| panic!("the handover leg reported no digest:\n{stdout}"));
    for phrase in [
        "duty handed to the standby pump at tick",
        "all-out annunciated",
        "inputs restored by tick",
    ] {
        assert!(
            handover_line.contains(phrase),
            "the handover digest names no '{phrase}' evidence: {handover_line}"
        );
    }
    assert!(
        stdout.contains("keeps-duty: reported, handover-failed")
            && stdout.contains("none-available-silent: reported, handover-failed"),
        "the handover leg's doctored cases did not report their named diagnostics:\n{stdout}"
    );
    // The pair contract's peer-announce leg ran and held: the foreign
    // ?peer= announce was refused while the checkpoint read answered,
    // the demoted peer reconverged tracking on its real successor, and
    // the landed-announce case reported its named diagnostic.
    let announce_line = stdout
        .lines()
        .find(|line| line.contains("peer-announce-digest"))
        .unwrap_or_else(|| panic!("the peer-announce leg reported no digest:\n{stdout}"));
    for phrase in [
        "checkpoint answered at tick",
        "tracking its successor",
        "roles restored",
    ] {
        assert!(
            announce_line.contains(phrase),
            "the peer-announce digest names no '{phrase}' evidence: {announce_line}"
        );
    }
    assert!(
        stdout.contains("landed-announce: reported, peer-announce-failed"),
        "the peer-announce leg's doctored case did not report its named diagnostic:\n{stdout}"
    );
    // The pair contract's failover leg ran and held: the declared
    // failover_budget armed the standby's --auto-promote, the severed
    // field owner left the surviving peer's miss run reported, the
    // self-promotion landed at the declared budget's boundary, the
    // plant's writer claim fenced foreign writes while the promoted
    // peer's writes landed, the run continued, the severed-standby
    // variant left the owner undisturbed, and the measurement run
    // walked the declared failover-select/on_bad_demand seam — the
    // degraded primary annunciating the managed backup-active alarm
    // while the chain controlled on the backup, the all-bad state
    // engaging the declared safe demand, the restores returning the
    // selection and the alarm — its digest line reports the evidence,
    // and the doctored cases reported the named diagnostic.
    let failover_line = stdout
        .lines()
        .find(|line| line.contains("failover-digest"))
        .unwrap_or_else(|| panic!("the failover leg reported no digest:\n{stdout}"));
    for phrase in [
        "self-promoted at tick",
        "persisted journal records",
        "undisturbed at tick",
        "annunciated at tick",
        "declared safe demand at tick",
        "re-selected the primary at tick",
    ] {
        assert!(
            failover_line.contains(phrase),
            "the failover digest names no '{phrase}' evidence: {failover_line}"
        );
    }
    for line in [
        "early-promotion: reported, failover-failed",
        "controls-on-bad: reported, failover-failed",
        "nonzero-fallback: reported, failover-failed",
    ] {
        assert!(
            stdout.contains(line),
            "the failover leg's doctored cases lack '{line}':\n{stdout}"
        );
    }
    // The failover declaration's deploy-stage divergences each
    // reported the named mismatch.
    for line in [
        "failover-flag-missing refused: rig-mismatch",
        "failover-flag-undeclared refused: rig-mismatch",
        "failover-wrong-peer refused: rig-mismatch",
    ] {
        assert!(
            stdout.contains(line),
            "the deploy stage's doctored pairs lack '{line}':\n{stdout}"
        );
    }
    // The pair contract's staged-vs-field divergence leg ran and held:
    // the withheld-pull window left the stale peer's served report
    // diverged naming the perturbed output, its promote refused
    // not_converged with no field hand-off, the resolution journaled
    // once the field matched again, and the write-free control window
    // reconverged and promoted — its digest line reports the evidence,
    // and the skipped-field-write case reported its named diagnostic.
    let divergence_line = stdout
        .lines()
        .find(|line| line.contains("divergence-digest"))
        .unwrap_or_else(|| panic!("the divergence leg reported no digest:\n{stdout}"));
    for phrase in [
        "diverged at tick",
        "naming point",
        "not_converged",
        "no field hand-off",
        "resolved at tick",
        "control window promoted at tick",
    ] {
        assert!(
            divergence_line.contains(phrase),
            "the divergence digest names no '{phrase}' evidence: {divergence_line}"
        );
    }
    assert!(
        stdout.contains("skip-field-write: reported, divergence-missed"),
        "the divergence leg's doctored case did not report its named diagnostic:\n{stdout}"
    );
    // The pair contract's report leg ran and held: the released
    // dcs-alarm-report computed the declared AlarmReport metric set
    // over the field owner's served journal and its manifest-declared
    // durable journal file — its digest line reports the evidence —
    // and each doctored case reported its named diagnostic.
    let report_line = stdout
        .lines()
        .find(|line| line.contains("report-digest"))
        .unwrap_or_else(|| panic!("the report leg reported no digest:\n{stdout}"));
    for phrase in ["alarm instances", "activations", "response pairs"] {
        assert!(
            report_line.contains(phrase),
            "the report digest names no '{phrase}' evidence: {report_line}"
        );
    }
    for tamper in ["expect-quiet", "unreachable-monitor", "unreadable-journal"] {
        assert!(
            stdout.contains(&format!("{tamper}: reported, report-failed")),
            "the report leg's {tamper} case did not report its named diagnostic:\n{stdout}"
        );
    }
    // The pair contract's managed-lifecycle leg ran and held: the
    // emitted model's managed-alarm surface exercised end to end on
    // the deployed pair — the field-driven activation, the attributed
    // ack, the bounded shelve and its auto-release, the named
    // never-shelvable refusal, and the designed suppression wiring —
    // its digest line reports the evidence, and each doctored
    // expectation reported its named diagnostic.
    let lifecycle_line = stdout
        .lines()
        .find(|line| line.contains("managed-lifecycle-digest"))
        .unwrap_or_else(|| panic!("the managed-lifecycle leg reported no digest:\n{stdout}"));
    for phrase in [
        "annunciated at tick",
        "acknowledged at tick",
        "shelved at tick",
        "released at tick",
        "restored at tick",
        "journal entries",
    ] {
        assert!(
            lifecycle_line.contains(phrase),
            "the managed-lifecycle digest names no '{phrase}' evidence: {lifecycle_line}"
        );
    }
    for tamper in ["expect-applied", "expect-standing"] {
        assert!(
            stdout.contains(&format!("{tamper}: reported, managed-lifecycle-failed")),
            "the managed-lifecycle leg's {tamper} case did not report its named diagnostic:\n{stdout}"
        );
    }
    // The pair contract's managed run-state carryover leg ran and
    // held: the managed alarm kinds' checkpointed run state carried
    // across the promotion — the mid-shelve countdown releasing at
    // its continued expiry rather than a restarted bound, the wired
    // out-of-service standing with evaluation held — its digest line
    // reports the evidence, and each doctored expectation reported
    // its named diagnostic.
    let carry_line = stdout
        .lines()
        .find(|line| line.contains("carry-digest"))
        .unwrap_or_else(|| panic!("the managed-carryover leg reported no digest:\n{stdout}"));
    for phrase in [
        "shelved at tick",
        "switched at tick",
        "released at tick",
        "declared bound",
        "out-of-service held",
        "roles restored at tick",
        "journal entries",
    ] {
        assert!(
            carry_line.contains(phrase),
            "the managed-carryover digest names no '{phrase}' evidence: {carry_line}"
        );
    }
    for tamper in ["restarted-bound", "dropped-oos"] {
        assert!(
            stdout.contains(&format!("{tamper}: reported, carry-failed")),
            "the managed-carryover leg's {tamper} case did not report its named diagnostic:\n{stdout}"
        );
    }
    // The pair contract's staging leg ran and held: the out-of-service
    // holds let the declared inflow raise the level unopposed through
    // the emitted threshold chain's declared crossings — the demand
    // staging 0→1→2, the high crossing annunciating the managed
    // high-level alarm with journaled evidence — the releases staging
    // the group inside the declared start delay, and the staged pumps
    // drawing the level down through the declared de-stage order — its
    // digest line reports the evidence, and each doctored expectation
    // reported its named diagnostic.
    let staging_line = stdout
        .lines()
        .find(|line| line.contains("staging-digest"))
        .unwrap_or_else(|| panic!("the staging leg reported no digest:\n{stdout}"));
    for phrase in [
        "demand 1 at tick",
        "2 at tick",
        "high annunciated at tick",
        "staged at tick",
        "pumped down by tick",
    ] {
        assert!(
            staging_line.contains(phrase),
            "the staging digest names no '{phrase}' evidence: {staging_line}"
        );
    }
    for tamper in ["wrong-demand", "immediate-lag"] {
        assert!(
            stdout.contains(&format!("{tamper}: reported, staging-failed")),
            "the staging leg's {tamper} case did not report its named diagnostic:\n{stdout}"
        );
    }
    // The pair contract's per-pump out-of-service leg ran and held:
    // the receipted `oos` write on the duty pump dropped its
    // availability and handed `duty` to the sibling inside the
    // declared wiring bound, the managed alarms reported the states
    // their declared lifecycle bindings select with `alarm` still
    // reporting process truth mid-OOS, and the false write returned
    // the pump to availability and the duty rotation — its digest
    // line reports the evidence, and each doctored expectation
    // reported its named diagnostic.
    let oos_line = stdout
        .lines()
        .find(|line| line.contains("oos-digest"))
        .unwrap_or_else(|| panic!("the out-of-service leg reported no digest:\n{stdout}"));
    for phrase in [
        "duty handed to the sibling at tick",
        "managed states at tick",
        "served at tick",
        "returned at tick",
        "acknowledged at tick",
        "rejoined at tick",
        "roles unmoved through tick",
        "journal entries",
    ] {
        assert!(
            oos_line.contains(phrase),
            "the out-of-service digest names no '{phrase}' evidence: {oos_line}"
        );
    }
    for tamper in ["keeps-duty", "managed-silent"] {
        assert!(
            stdout.contains(&format!("{tamper}: reported, oos-failed")),
            "the out-of-service leg's {tamper} case did not report its named diagnostic:\n{stdout}"
        );
    }
    // The pair contract's power-fail interlock leg ran and held: the
    // driven `power-fail` contact dropped `power-ok` and both pumps'
    // availability, the motor commands released while the chain's
    // demand still stood, `none-available` and the managed `power-fail`
    // alarm annunciated with journaled evidence, the receipted
    // `power-fail-ack` cleared the latch mid-condition, and the
    // released contact re-staged the standing demand inside the
    // declared bounds — its digest line reports the evidence, and each
    // doctored expectation reported its named diagnostic.
    let power_trip_line = stdout
        .lines()
        .find(|line| line.contains("power-trip-digest"))
        .unwrap_or_else(|| panic!("the power-fail interlock leg reported no digest:\n{stdout}"));
    for phrase in [
        "tracking by tick",
        "full demand at tick",
        "tripped at tick",
        "acknowledged at tick",
        "permissives returned at tick",
        "re-staged by tick",
        "run continued to tick",
    ] {
        assert!(
            power_trip_line.contains(phrase),
            "the power-trip digest names no '{phrase}' evidence: {power_trip_line}"
        );
    }
    for tamper in ["commands-standing", "availability-holds"] {
        assert!(
            stdout.contains(&format!("{tamper}: reported, power-trip-failed")),
            "the power-fail interlock leg's {tamper} case did not report its named diagnostic:\n{stdout}"
        );
    }
    assert!(
        stdout.contains("== consumers =="),
        "the consumers stage did not run:\n{stdout}"
    );
    assert!(
        stdout.contains("identical across every schedule and both passes"),
        "the consumer schedules did not produce identical digests:\n{stdout}",
    );
    assert!(
        stdout.contains("== ctl =="),
        "the ctl stage did not run:\n{stdout}"
    );
    let ctl_line = stdout
        .lines()
        .find(|line| line.contains("ctl-digest") && line.contains("identical"))
        .unwrap_or_else(|| panic!("the ctl stage reported no digest:\n{stdout}"));
    for phrase in ["receipted submissions", "named refusals"] {
        let index = ctl_line
            .find(phrase)
            .unwrap_or_else(|| panic!("the ctl digest names no '{phrase}' count: {ctl_line}"));
        let count: usize = ctl_line[..index]
            .split_whitespace()
            .next_back()
            .and_then(|token| token.parse().ok())
            .unwrap_or_else(|| panic!("the '{phrase}' count is not a number: {ctl_line}"));
        assert!(count > 0, "the ctl stage proved no {phrase}: {ctl_line}");
    }
}

/// The `upgrade` stage is the executable assertion of the documented
/// repin upgrade (README §7): under the same `file://`-remote and
/// binary substitutions as the other stages, the stage repins the
/// unchanged tree to the checkout's `HEAD`, proves the emitted
/// `model/plant.json` is byte-identical across the repin
/// (`emit-divergent` stands guard), re-runs the full pipeline under the
/// repin, and refuses the named incompatible crossings — the
/// nonexistent tag (`pin-unresolvable`) and the pin outside the
/// supported `MODEL_VERSION`/`version` window (`crossing-unrefused`).
#[test]
fn the_upgrade_stage_proves_the_repin_and_the_named_crossings() {
    let tools = build_tools();
    let copy = Materialized::new();
    let output = copy.check(&tools);
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        output.status.success(),
        "the template's ci/check.sh failed:\nstdout:\n{stdout}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        stdout.contains("== upgrade =="),
        "the upgrade stage did not run:\n{stdout}"
    );
    assert!(
        stdout.contains("byte-identical emit across the repin"),
        "the repin did not emit byte-identically:\n{stdout}"
    );
    assert!(
        stdout.contains("the full pipeline passes under the repin"),
        "the repinned pipeline did not pass:\n{stdout}"
    );
    assert!(
        stdout.contains("pin-unresolvable") && stdout.contains("outside MODEL_VERSION"),
        "the named incompatible crossings were not exercised:\n{stdout}"
    );
}

/// A checked-in artifact that no longer matches a fresh emit is the
/// `stale-artifact` diagnostic.
#[test]
fn a_checked_in_model_drift_reports_stale_artifact() {
    let tools = build_tools();
    let copy = Materialized::new();
    let model = copy.dir.join("model/plant.json");
    let source = std::fs::read_to_string(&model).unwrap();
    std::fs::write(
        &model,
        source.replacen("\"version\": 1", "\"version\": 0", 1),
    )
    .unwrap();
    let output = copy.check(&tools);
    assert!(!output.status.success(), "a drifted artifact passed");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("stale-artifact"),
        "expected the stale-artifact diagnostic, got:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

/// A manifest fingerprint the emitted model no longer matches is the
/// `manifest-fingerprint-mismatch` diagnostic.
#[test]
fn a_wrong_manifest_fingerprint_reports_the_named_diagnostic() {
    let tools = build_tools();
    let copy = Materialized::new();
    let manifest = copy.dir.join("deploy/manifest.json");
    let source = std::fs::read_to_string(&manifest).unwrap();
    let parsed: serde_json::Value = serde_json::from_str(&source).unwrap();
    let recorded = parsed["model"]["fingerprint"].as_str().unwrap();
    let field = format!("\"fingerprint\": \"{recorded}\"");
    assert!(
        source.contains(&field),
        "the manifest's fingerprint field moved"
    );
    std::fs::write(
        &manifest,
        source.replacen(&field, "\"fingerprint\": \"ffffffffffffffff\"", 1),
    )
    .unwrap();
    let output = copy.check(&tools);
    assert!(!output.status.success(), "a wrong fingerprint passed");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("manifest-fingerprint-mismatch"),
        "expected the manifest-fingerprint-mismatch diagnostic, got:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

/// A rig definition diverging from the deployment manifest is the
/// `rig-mismatch` diagnostic — exercised against a doctored copy at
/// script level, so neither the remote stand-in nor the tooling builds
/// are needed.
#[test]
fn a_divergent_rig_definition_reports_rig_mismatch() {
    let dir = std::env::temp_dir().join(format!(
        "dcs-reference-plant-rig-{}-{:?}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    copy_tree(&root().join("reference-plant"), &dir);
    let compose = dir.join("deploy/compose.yaml");
    let source = std::fs::read_to_string(&compose).unwrap();
    std::fs::write(&compose, source.replacen("ctrl-a:8080", "ctrl-a:9090", 1)).unwrap();
    let output = Command::new("python3")
        .arg("ci/deploy_rig.py")
        .current_dir(&dir)
        .output()
        .expect("python3 runs the rig-definition check");
    let _ = std::fs::remove_dir_all(&dir);
    assert!(
        !output.status.success(),
        "a divergent rig definition passed"
    );
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("rig-mismatch"),
        "expected the rig-mismatch diagnostic, got:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

/// A standby wired at a peer that never serves is the `pair-failed`
/// diagnostic — exercised against a copied tree at script level with
/// the locally built tooling, so the remote stand-in is not needed.
/// The driver's `broken-peer-flag` tamper wires the tracking peer's
/// `--standby` flag at an address nothing serves; the leg must refuse
/// the run naming the lost convergence, which the check reports as
/// `pair-failed` — never a silently unconverged pass.
#[test]
fn a_broken_peer_flag_reports_pair_failed() {
    let tools = build_tools();
    let dir = std::env::temp_dir().join(format!(
        "dcs-reference-plant-pair-{}-{:?}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    copy_tree(&root().join("reference-plant"), &dir);
    let output = Command::new("python3")
        .arg("ci/pair.py")
        .arg("--plant-server")
        .arg(tools.join("dcs-plant-server"))
        .arg("--controller")
        .arg(tools.join("dcs-controller"))
        .args([
            "--model",
            "model/plant.json",
            "--dynamics",
            "model/dynamics.json",
            "--scenario",
            "ci/scenario.json",
            "--manifest",
            "deploy/manifest.json",
            "--tamper",
            "broken-peer-flag",
        ])
        .current_dir(&dir)
        .output()
        .expect("python3 runs the redundant-pair leg");
    let _ = std::fs::remove_dir_all(&dir);
    assert!(
        !output.status.success(),
        "a broken peer flag passed the pair leg"
    );
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("never reported tracking"),
        "expected the lost-convergence evidence, got:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

/// Requiring convergence on the negotiation leg's foreign-model
/// standby is the `negotiation-failed` evidence — exercised against a
/// copied tree at script level with the locally built tooling, so the
/// remote stand-in is not needed. The driver's `expect-tracking`
/// tamper flips the observation window's assertion to require
/// `tracking`; the leg must refuse the pass naming the degraded
/// negotiation state the peer actually reported — never a silently
/// accepted run.
#[test]
fn a_wrong_negotiation_expectation_reports_the_degraded_state() {
    let tools = build_tools();
    let dir = std::env::temp_dir().join(format!(
        "dcs-reference-plant-negotiation-{}-{:?}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    copy_tree(&root().join("reference-plant"), &dir);
    let output = Command::new("python3")
        .arg("ci/negotiation.py")
        .arg("--plant-server")
        .arg(tools.join("dcs-plant-server"))
        .arg("--controller")
        .arg(tools.join("dcs-controller"))
        .args([
            "--model",
            "model/plant.json",
            "--dynamics",
            "model/dynamics.json",
            "--scenario",
            "ci/scenario.json",
            "--manifest",
            "deploy/manifest.json",
            "--tamper",
            "expect-tracking",
        ])
        .current_dir(&dir)
        .output()
        .expect("python3 runs the checkpoint-negotiation leg");
    let _ = std::fs::remove_dir_all(&dir);
    assert!(
        !output.status.success(),
        "a wrong convergence expectation passed the negotiation leg"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("never reported tracking") && stderr.contains("degraded"),
        "expected the degraded negotiation report the peer served, got:\n{stderr}"
    );
}

/// A doctored refusal leg asserting the standby-directed write settles
/// `applied` is the `refusal-failed` diagnostic — exercised against a
/// copied tree at script level with the locally built tooling, the
/// same seam the broken-peer-flag test uses. The driver's
/// `expect-applied` tamper flips the leg's own expectation; the
/// tracking standby's honest `not_active` rejection must fail it
/// naming the actual answer — never a silently unrefused pass.
#[test]
fn a_doctored_write_expectation_reports_refusal_failed() {
    let tools = build_tools();
    let dir = std::env::temp_dir().join(format!(
        "dcs-reference-plant-refusal-{}-{:?}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    copy_tree(&root().join("reference-plant"), &dir);
    let output = Command::new("python3")
        .arg("ci/refusal.py")
        .arg("--plant-server")
        .arg(tools.join("dcs-plant-server"))
        .arg("--controller")
        .arg(tools.join("dcs-controller"))
        .args([
            "--model",
            "model/plant.json",
            "--dynamics",
            "model/dynamics.json",
            "--scenario",
            "ci/scenario.json",
            "--manifest",
            "deploy/manifest.json",
            "--tamper",
            "expect-applied",
        ])
        .current_dir(&dir)
        .output()
        .expect("python3 runs the role-gated refusal leg");
    let _ = std::fs::remove_dir_all(&dir);
    assert!(
        !output.status.success(),
        "a doctored write expectation passed the refusal leg"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("expected an applied receipt") && stderr.contains("not_active"),
        "expected the named not_active evidence, got:\n{stderr}"
    );
}

/// A doctored handover leg expecting the failed pump to keep `duty`,
/// or `none_available` never to report once every pump is out, is the
/// `handover-failed` diagnostic — exercised against a copied tree at
/// script level with the locally built tooling, the same seam the
/// refusal test uses. The driver's `keeps-duty` and
/// `none-available-silent` tampers flip the leg's own expectations;
/// the pair's honest handover must fail each naming the actual
/// evidence — never a silently wrong pass.
#[test]
fn a_doctored_handover_expectation_reports_handover_failed() {
    let tools = build_tools();
    let dir = std::env::temp_dir().join(format!(
        "dcs-reference-plant-handover-{}-{:?}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    copy_tree(&root().join("reference-plant"), &dir);
    for (tamper, evidence) in [
        ("keeps-duty", "keep duty"),
        ("none-available-silent", "never to report"),
    ] {
        let output = Command::new("python3")
            .arg("ci/handover.py")
            .arg("--plant-server")
            .arg(tools.join("dcs-plant-server"))
            .arg("--controller")
            .arg(tools.join("dcs-controller"))
            .args([
                "--model",
                "model/plant.json",
                "--dynamics",
                "model/dynamics.json",
                "--scenario",
                "ci/scenario.json",
                "--manifest",
                "deploy/manifest.json",
                "--tamper",
                tamper,
            ])
            .current_dir(&dir)
            .output()
            .expect("python3 runs the failure-handover leg");
        assert!(
            !output.status.success(),
            "a doctored {tamper} expectation passed the handover leg"
        );
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            stderr.contains(evidence),
            "expected the named {tamper} evidence, got:\n{stderr}"
        );
    }
    let _ = std::fs::remove_dir_all(&dir);
}

/// A doctored takeover leg asserting the pump still follows the
/// group while `mode` stands manual is the `takeover-failed`
/// diagnostic — exercised against a copied tree at script level with
/// the locally built tooling, the same seam the broken-peer-flag and
/// refusal tests use. The driver's `follows-group` tamper flips the
/// leg's own expectation; the honest manual selection — the auto leg
/// reporting manual with the delivered command off the group's
/// request — must fail it naming the actual readings, never a
/// silently unexercised pass.
#[test]
fn a_doctored_follows_group_expectation_reports_takeover_failed() {
    let tools = build_tools();
    let dir = std::env::temp_dir().join(format!(
        "dcs-reference-plant-takeover-{}-{:?}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    copy_tree(&root().join("reference-plant"), &dir);
    let output = Command::new("python3")
        .arg("ci/takeover.py")
        .arg("--plant-server")
        .arg(tools.join("dcs-plant-server"))
        .arg("--controller")
        .arg(tools.join("dcs-controller"))
        .args([
            "--model",
            "model/plant.json",
            "--dynamics",
            "model/dynamics.json",
            "--scenario",
            "ci/scenario.json",
            "--manifest",
            "deploy/manifest.json",
            "--tamper",
            "follows-group",
        ])
        .current_dir(&dir)
        .output()
        .expect("python3 runs the manual-takeover leg");
    let _ = std::fs::remove_dir_all(&dir);
    assert!(
        !output.status.success(),
        "a doctored follows-group expectation passed the takeover leg"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("did not follow the group"),
        "expected the named follows-group evidence, got:\n{stderr}"
    );
}

/// A doctored divergence leg skipping the field-side write while the
/// diverged report is still asserted is the `divergence-missed`
/// diagnostic — exercised against a copied tree at script level with
/// the locally built tooling, the same seam the broken-peer-flag,
/// refusal, and takeover tests use. The driver's `skip-field-write`
/// tamper withholds the plant-protocol write; the honest
/// resumed-checkpoint comparison finds the staged image matching the
/// untouched field, so the leg must fail naming the report it
/// expected — never a silently unconvinced pass.
#[test]
fn a_skipped_field_write_reports_divergence_missed() {
    let tools = build_tools();
    let dir = std::env::temp_dir().join(format!(
        "dcs-reference-plant-divergence-{}-{:?}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    copy_tree(&root().join("reference-plant"), &dir);
    let output = Command::new("python3")
        .arg("ci/divergence.py")
        .arg("--plant-server")
        .arg(tools.join("dcs-plant-server"))
        .arg("--controller")
        .arg(tools.join("dcs-controller"))
        .args([
            "--model",
            "model/plant.json",
            "--dynamics",
            "model/dynamics.json",
            "--scenario",
            "ci/scenario.json",
            "--manifest",
            "deploy/manifest.json",
            "--tamper",
            "skip-field-write",
        ])
        .current_dir(&dir)
        .output()
        .expect("python3 runs the staged-vs-field divergence leg");
    let _ = std::fs::remove_dir_all(&dir);
    assert!(
        !output.status.success(),
        "a skipped field-side write passed the divergence leg"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("expected the diverged report"),
        "expected the named diverged-report evidence, got:\n{stderr}"
    );
}

/// A served registry document diverging from the recorded artifact's
/// declared structure is the `schema-mismatch` diagnostic — exercised
/// at script level against the consumer-side conformance check, the
/// same seam the rig-definition test uses. The artifact is the release
/// record's checked-in `block-interfaces.schema.json`; a structurally
/// conforming document passes, a missing required field fails, and a
/// mistyped required field fails.
#[test]
fn a_structurally_divergent_served_document_reports_schema_mismatch() {
    let dir = std::env::temp_dir().join(format!(
        "dcs-reference-plant-schema-{}-{:?}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let schema = root().join("docs/releases/v0.2.0/block-interfaces.schema.json");
    let script = root().join("reference-plant/ci/schema_conformance.py");
    let conforming = serde_json::json!({
        "publication": 0,
        "tick": 0,
        "interfaces": [{
            "name": "kind:1",
            "interface": {
                "version": 1,
                "kind": "kind",
                "measurements": [],
                "configuration": [],
                "state": [],
                "commands": [],
                "events": [],
            },
        }],
    });
    let conform = |document: &serde_json::Value| {
        let path = dir.join("document.json");
        std::fs::write(&path, serde_json::to_string(document).unwrap()).unwrap();
        Command::new("python3")
            .arg(&script)
            .arg("--schema")
            .arg(&schema)
            .arg("--document")
            .arg(&path)
            .output()
            .expect("python3 runs the schema-conformance check")
    };
    let output = conform(&conforming);
    assert!(
        output.status.success(),
        "a conforming document failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    // A required top-level field dropped.
    let mut missing = conforming.clone();
    missing.as_object_mut().unwrap().remove("tick");
    let output = conform(&missing);
    assert!(
        !output.status.success(),
        "a document missing a required field passed"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("schema-mismatch") && stderr.contains("tick"),
        "expected the schema-mismatch diagnostic naming the field, got:\n{stderr}"
    );
    // A required field mistyped — a string where the schema declares
    // an integer.
    let mut mistyped = conforming.clone();
    mistyped["tick"] = serde_json::json!("not-a-tick");
    let output = conform(&mistyped);
    assert!(
        !output.status.success(),
        "a document mistyping a required field passed"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("schema-mismatch") && stderr.contains("tick"),
        "expected the schema-mismatch diagnostic naming the field, got:\n{stderr}"
    );
    // A nested required field dropped inside a served interface.
    let mut nested = conforming.clone();
    nested["interfaces"][0]["interface"]
        .as_object_mut()
        .unwrap()
        .remove("events");
    let output = conform(&nested);
    assert!(
        !output.status.success(),
        "a document missing a nested required field passed"
    );
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("schema-mismatch"),
        "expected the schema-mismatch diagnostic, got:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let _ = std::fs::remove_dir_all(&dir);
}

/// A dynamics document the scenario's declared outcomes no longer hold
/// against is the `scenario-failed` diagnostic.
#[test]
fn a_changed_trajectory_reports_scenario_failed() {
    let tools = build_tools();
    let copy = Materialized::new();
    let dynamics = copy.dir.join("model/dynamics.json");
    let source = std::fs::read_to_string(&dynamics).unwrap();
    // The declared inflow stops: the well never fills and the
    // staging legs' expectations fail.
    std::fs::write(
        &dynamics,
        source.replacen("\"off_rate\": 0.25", "\"off_rate\": 0.0", 1),
    )
    .unwrap();
    let output = copy.check(&tools);
    assert!(!output.status.success(), "a broken scenario passed");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("scenario-failed"),
        "expected the scenario-failed diagnostic, got:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

/// The `surface` stage's tamper case, at script level. Every edit to
/// the materialized tree that would desynchronize the served surface
/// from the declared one is already caught by an earlier stage — emit
/// proves the checked-in artifacts byte-equal a fresh emit, and the
/// controller serves the same document the stage derives its
/// expectations from — so this exercises the stage's comparison
/// directly: a served index missing a declared writable command point,
/// a writable point served read-only, or the never-shelvable alarm's
/// read-only `shelve` surface served writable each report the named
/// mismatches `ci/check.sh` turns into `surface-mismatch`.
#[test]
fn a_tampered_served_index_reports_named_mismatches() {
    let output = Command::new("python3")
        .arg("-c")
        .arg(
            r#"
import importlib.util
import json

spec = importlib.util.spec_from_file_location("simulate", "ci/simulate.py")
simulate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(simulate)

model = json.load(open("model/plant.json"))
declared = simulate.declared_signal_index(model)

# The untampered index reports no mismatches.
served = json.loads(json.dumps(declared))
assert simulate.index_mismatches(declared, served) == []

# A declared writable command point dropped from the served index is
# named as not served.
served = json.loads(json.dumps(declared))
writable = next(e["point"] for e in served["points"] if e["writable"])
served["points"] = [e for e in served["points"] if e["point"] != writable]
failures = simulate.index_mismatches(declared, served)
assert any(str(writable) in f and "not served" in f for f in failures), failures

# A writable command point served read-only is flagged.
served = json.loads(json.dumps(declared))
entry = next(e for e in served["points"] if e["point"] == writable)
entry["writable"] = False
failures = simulate.index_mismatches(declared, served)
assert any(str(writable) in f and "writable" in f for f in failures), failures

# The never-shelvable high-level alarm's read-only shelve point served
# writable is flagged — the refused shelve surface.
served = json.loads(json.dumps(declared))
entry = next(e for e in served["points"] if e["name"] == "lah-shelve")
entry["writable"] = True
failures = simulate.index_mismatches(declared, served)
assert any("lah-shelve" in f and "writable" in f for f in failures), failures

print("tamper cases report named mismatches")
"#,
        )
        .current_dir(root().join("reference-plant"))
        .output()
        .expect("python3 runs the surface comparison");
    assert!(
        output.status.success(),
        "the surface comparison did not flag the tampered index:\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

/// The `surface` stage's registry, declared-command, and emitted-event
/// tamper cases — the same script-level seam the index tamper test
/// uses, since every tree edit desynchronizing the served surface from
/// the declared one is caught by the earlier emit and fingerprint
/// stages. A served registry missing a declared component, drifting a
/// kind, or dropping a declared resource; a declared command answering
/// no receipt, a refused one, never settling `applied`, or serving an
/// availability verdict adrift of the published one (in either
/// direction, or refused without the kind's named reason); and a
/// kind-emitted event absent from the journal or the per-instance
/// resource view each report the named mismatches `ci/check.sh` turns
/// into `surface-mismatch`.
#[test]
fn a_tampered_served_interface_reports_named_mismatches() {
    let output = Command::new("python3")
        .arg("-c")
        .arg(
            r#"
import importlib.util
import json

spec = importlib.util.spec_from_file_location("simulate", "ci/simulate.py")
simulate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(simulate)

model = json.load(open("model/plant.json"))

# A served-shaped registry document built from the stage's own
# model-derived expectations — the untampered shape reports no
# mismatches.
def served_schema():
    interfaces = []
    for name, want in simulate.registry_expectations(model).items():
        interfaces.append({
            "name": name,
            "interface": {
                "kind": want["kind"],
                "version": 1,
                "measurements": [
                    {"name": port, **fields}
                    for port, fields in want["ports"].items()
                ],
                "state": [],
                "configuration": [
                    {"name": param, "kind": kind}
                    for param, kind in want["configuration"].items()
                ],
                "commands": [
                    {"name": command, **fields}
                    for command, fields in want["commands"].items()
                ],
                "events": [
                    {"name": event} for event in sorted(want["events"])
                ],
            },
        })
    return {"interfaces": interfaces}

assert simulate.schema_mismatches(model, served_schema()) == []

# A declared component the registry misses is named.
schema = served_schema()
missing = schema["interfaces"].pop()["name"]
failures = simulate.schema_mismatches(model, schema)
assert any(missing in f for f in failures), failures

# A served kind drifting from the declared kind is named.
schema = served_schema()
entry = schema["interfaces"][0]
entry["interface"]["kind"] = "no-such-kind"
failures = simulate.schema_mismatches(model, schema)
assert any(entry["name"] in f and "no-such-kind" in f for f in failures), failures

# A declared port serving no resource is named.
schema = served_schema()
entry = next(e for e in schema["interfaces"] if e["interface"]["measurements"])
port = entry["interface"]["measurements"].pop(0)["name"]
failures = simulate.schema_mismatches(model, schema)
assert any(entry["name"] in f and port in f for f in failures), failures

# A declared adapted command missing from the registry is named.
schema = served_schema()
entry = next(e for e in schema["interfaces"] if e["interface"]["commands"])
command = entry["interface"]["commands"].pop(0)["name"]
failures = simulate.schema_mismatches(model, schema)
assert any(entry["name"] in f and command in f for f in failures), failures

# A declared adapted event missing from the registry is named.
schema = served_schema()
entry = schema["interfaces"][0]
event = entry["interface"]["events"].pop(0)["name"]
failures = simulate.schema_mismatches(model, schema)
assert any(entry["name"] in f and event in f for f in failures), failures

# A declared command producing no receipt is named.
command = {"name": "advance", "request": [], "adapted": "declared"}
failures = simulate.receipt_mismatches("sequencer:39", command, None)
assert any("no receipt" in f for f in failures), failures

# A declared command's refused receipt is named.
failures = simulate.receipt_mismatches(
    "sequencer:39",
    command,
    {
        "command": {
            "invoke": {"component": "sequencer:39", "command": "advance"}
        },
        "outcome": {"rejected": {"reason": {"command_refused": {}}}},
    },
)
assert any("command_refused" in f for f in failures), failures

# A declared command that never settles `applied` is named.
failures = simulate.settlement_misses([("sequencer:39", "advance")], [])
assert any("no settled receipt" in f for f in failures), failures

# A kind-emitted event absent from the journal is named.
event = {
    "name": "step_completed",
    "payload": [{"name": "step", "kind": "int"}],
    "adapted": "declared",
}
failures = simulate.emitted_event_misses([("sequencer:39", event)], [])
assert any("step_completed" in f for f in failures), failures

# A kind-emitted event missing from the instance's resource view is
# named.
failures = simulate.resource_event_misses(
    [("sequencer:39", event)],
    {"components": [{"name": "sequencer:39", "events": []}]},
)
assert any(
    "step_completed" in f and "sequencer:39" in f for f in failures
), failures

# A kind-declared command's served availability verdict: the honest
# states report no mismatches in either direction, an invocable verdict
# served unavailable (and the reverse) is named, and a refused verdict
# served without the kind's named reason is named.
command = {
    "name": "advance",
    "request": [{"name": "count", "kind": "int"}],
    "adapted": "declared",
    "availability": "kind_declared",
}
def resources_with(state):
    return {"components": [
        {"name": "sequencer:39", "commands": [dict({"name": "advance"}, **state)]}
    ]}

assert simulate.availability_misses(
    [("sequencer:39", command)],
    resources_with({"available": True}),
    {"sequencer:39": True},
) == []
assert simulate.availability_misses(
    [("sequencer:39", command)],
    resources_with({"available": False, "refusal": "the sequence has run to its end; reset restarts it"}),
    {"sequencer:39": False},
) == []
failures = simulate.availability_misses(
    [("sequencer:39", command)],
    resources_with({"available": False, "refusal": "not now"}),
    {"sequencer:39": True},
)
assert any("advance" in f and "expected True" in f for f in failures), failures
failures = simulate.availability_misses(
    [("sequencer:39", command)],
    resources_with({"available": True}),
    {"sequencer:39": False},
)
assert any("advance" in f and "expected False" in f for f in failures), failures
failures = simulate.availability_misses(
    [("sequencer:39", command)],
    resources_with({"available": False}),
    {"sequencer:39": False},
)
assert any("advance" in f and "named refusal" in f for f in failures), failures

print("registry, receipt, and event tamper cases report named mismatches")
"#,
        )
        .current_dir(root().join("reference-plant"))
        .output()
        .expect("python3 runs the surface comparison");
    assert!(
        output.status.success(),
        "the surface comparison did not flag the tampered registry:\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

/// The release contract's named-diagnostic vocabulary must resolve
/// every `<leg>-unchecked` self-check diagnostic `ci/check.sh` emits —
/// an operator or tool reading a self-check failure resolves the name
/// against the declared contract, so an emitted name the vocabulary
/// does not declare is an unnamed diagnostic by another name. An
/// emitted `<stem>-unchecked` is covered when the contract declares
/// the name itself, or when the declared `<leg>-unchecked` convention
/// covers it — the convention entry present and the leg's own
/// `<stem>` or `<stem>-failed` diagnostic declared.
#[test]
fn the_contract_declares_every_emitted_unchecked_diagnostic() {
    let check = std::fs::read_to_string(root().join("reference-plant/ci/check.sh")).unwrap();
    let contract = std::fs::read_to_string(root().join("docs/release-contract.md")).unwrap();
    let declared = |name: &str| contract.contains(&format!("`{name}`"));
    // The emitted set: every `fail "<name>-unchecked:` the script can
    // report — the first token of a fail message is its diagnostic.
    let mut emitted: Vec<String> = Vec::new();
    for line in check.lines() {
        let mut rest = line;
        while let Some(start) = rest.find("fail \"") {
            rest = &rest[start + "fail \"".len()..];
            let name: String = rest
                .chars()
                .take_while(|c| c.is_ascii_alphanumeric() || *c == '-')
                .collect();
            if name.ends_with("-unchecked") && !emitted.contains(&name) {
                emitted.push(name);
            }
        }
    }
    assert!(
        !emitted.is_empty(),
        "ci/check.sh emits no -unchecked self-check diagnostics"
    );
    // The convention entry itself must be declared for it to cover a
    // name the vocabulary does not enumerate verbatim.
    let convention = declared("<leg>-unchecked");
    for name in &emitted {
        let stem = name.strip_suffix("-unchecked").unwrap();
        assert!(
            declared(name)
                || (convention && (declared(stem) || declared(&format!("{stem}-failed")))),
            "the contract's named-diagnostic vocabulary covers neither \
             the emitted {name} nor its leg"
        );
    }
}
