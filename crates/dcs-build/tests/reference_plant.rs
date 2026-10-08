//! The independent-consumer proof (decision 79): the
//! `reference-plant/` tree in this repository is a verbatim-publishable
//! consumer repository. This test materializes it into a scratch
//! directory *outside* the workspace, rewrites only the dependency
//! remote to a `file://` stand-in for the published origin — in the
//! manifest and, with it, in the committed `Cargo.lock`, so the
//! recorded `rev`/`tag` pin and the revision it resolves to stay
//! exactly as shipped while the stand-in serves that very revision —
//! and runs the tree's own `ci/check.sh`
//! end to end: the committed lockfile's agreement with the declared
//! pin — every `dcs-*` record of a released crate carried by a git
//! source, a path package's sourceless record named
//! `path-dependency-leak`, a manifest declaring only `dcs-build`
//! directly — the contract's optional direct `dcs-core`, `dcs-model`
//! declarations omitted — passing on the lockfile recording all three
//! release crates at that pin, and a tag re-pointed after the cut with
//! the lockfile regenerated to follow it named `lockfile-stale`
//! against the release record's `Commit` field — a tag query the
//! remote cannot answer refused rather than read as the tag's
//! absence, and every spelling Cargo resolves afresh in `rev =` — a
//! branch name, a short sha, a tag name — held to the revision the
//! remote serves under that name rather than admitted uncompared —
//! `cargo fetch
//! --locked` resolving without a
//! re-resolve,
//! byte-identical emit against the checked-in artifacts,
//! released-tooling acceptance — plus the contract's remaining
//! `dcs-model` surfaces: `schema`, `interface-schema`, and
//! `deploy-schema` emissions — and the released `dcs-plant-server`'s
//! `--dynamics-schema` emission — byte-pinned to the release record's
//! artifacts fetched through the stand-in remote at the pinned rev,
//! the checked-in deployment manifest and dynamics document screened
//! against their declared schema artifacts — a schema-violating
//! doctored copy of each reporting `schema-mismatch` —
//! `diff` legs over a doctored
//! compatible revision and the identical document, and
//! `summary`/`signal-index` recorded as run evidence — the
//! alarm-validation leg proving the released `dcs-controller --check`
//! refuses a customer-owned document whose managed-alarm record is
//! broken, naming the missing element — the manifest
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
//! control window reconverging and promoting normally — plus the pair
//! contract's emit-identical leg: with the standby tracking, the
//! sequencer's counted `step_completed` emissions must serve
//! identical routed event records through both peers'
//! `GET /resources` views — the `consumers`
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
//! nonzero — the pair contract's demote-boundary pending-command
//! leg, which admits a receipted write on the field owner and demotes
//! inside its pending window: the demoted peer's first quiesced scan
//! audited for no phantom `command_settled` and no vanished pending
//! receipt, the admission settling exactly once — carried `applied`
//! or `Rejected{superseded}` — across both peers' journals, receipt
//! logs, images, and durable files, and the pair's launch roles
//! restored — the pair contract's managed-lifecycle leg, which
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
//! bounds with the pair's roles unchanged — the pair contract's
//! alarm-rationalization leg, which asserts the emitted model's
//! managed alarm instances' declared-once record verbatim on both
//! peers' served surfaces — `GET /signals`' components section
//! carrying each instance's `rationalization` block, `GET
//! /snapshot`'s parameters section serving each alarm's declared
//! `priority`/`class`/`response_ticks` live — before and after the
//! documented `demote`/`promote` switch, and the pair's launch roles
//! restored — the pair contract's claim-fencing leg, which attaches a
//! dedicated third sim-net client to the settled pair's spawned plant
//! and exercises the standing writer claim's whole lifecycle: fenced
//! `write`/`step` probes plus the same mutations through the shipped
//! `dcs-plant-ctl`, the `ensure_writer`/`release_writer` verbs under
//! foreign and owner tokens, and a rogue `claim_writer` resolving per
//! the settled contract — its preempt's journaled `field_claim_lost`
//! and in-place demotion on the superseded owner, then the pair
//! restored to its launch roles — the pair contract's
//! commissioning/handover record leg, which materializes the declared
//! commissioning record from one deterministic driven run: the field
//! census audited against the declared channel set (the I/O checkout
//! record), the measurement ladder and the receipted output loop (the
//! loop-check evidence), every managed alarm's declared record served
//! verbatim on both peers (the alarm rationalization sign-off), the
//! documented `demote`/`promote` switch and restore (the handover
//! procedure), and the document set digested with each peer's
//! checkpoint fingerprint and durable files (the documentation
//! turnover) — the completeness audit naming any missing artifact,
//! two passes producing identical digests — the pair contract's
//! in-service revision-roll leg, which emits the consumer's own
//! revision-2 composition and rolls it through a `--revised` standby:
//! the named crossing with its carryover report, the served
//! fingerprint advancing, the switch settling the revised peer
//! `active` with control continuing, and the incompatible revision
//! refused with its named diagnostic — and the pair contract's
//! configuration-backup and restore leg, which backs the running pair
//! up beside the live deployment, audits the set whole, wipes the
//! volumes, restores onto fresh ones, and resumes each peer at its
//! persisted tick with the receipt log and journal order continuing —
//! and the `upgrade` stage,
//! which materializes the tree at the previous release's recorded rev
//! (seeded into the stand-in beside the tag) and repins it to the
//! recorded release — the stand-in's tag naming the commit the
//! committed `Cargo.lock` records for it, the commit the release
//! record's `Commit` field carries — re-running the full pipeline
//! under the repin.
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
//! `schema-mismatch`, `schema-mismatch-nondeterministic`,
//! `diff-mismatch`, `pair-failed`,
//! `negotiation-failed`, `refusal-failed`, `handover-failed`,
//! `takeover-failed`,
//! `peer-announce-failed`,
//! `divergence-missed`/`divergence-nondeterministic`,
//! `report-failed`/`report-nondeterministic`,
//! `managed-lifecycle-failed`/`managed-lifecycle-nondeterministic`,
//! `event-parity-failed`/`event-parity-nondeterministic`,
//! `commissioning-failed`/`commissioning-nondeterministic`/
//! `commissioning-unchecked`,
//! `revision-roll-failed`/`revision-roll-nondeterministic`/
//! `revision-roll-unchecked`,
//! `backup-restore-failed`/`backup-restore-nondeterministic`/
//! `backup-restore-unchecked`, `lockfile-stale`,
//! `path-dependency-leak`, and the
//! `surface-mismatch` paths
//! a drifting interface registry, a receiptless declared command, or an
//! unobserved emitted event each produce.

mod common;

use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::{Duration, Instant};

use common::{CARGO, PIN_UNRESOLVABLE, root};

/// The remote the published tree records — the string the materialized
/// copy's `Cargo.toml` rewrites to the `file://` stand-in.
const PUBLISHED_REMOTE: &str = "https://github.com/Jan-Kaspar1/dcs.git";

/// Bound for one network fetch attempt in `serve_pinned_rev`'s seed
/// fallback: a stalled remote must fail the test naming the remote,
/// never hang the `rust-tests` leg to the platform's job cap.
/// Overridable via `DCS_FETCH_TIMEOUT_SECS` for harness-level fakes.
fn fetch_timeout() -> Duration {
    std::env::var("DCS_FETCH_TIMEOUT_SECS")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .map(Duration::from_secs)
        .unwrap_or_else(|| Duration::from_secs(120))
}

/// Bound for the nested `ci/check.sh` run: the stage's baseline is
/// ~30min but shared CI runners under parallel shards have needed well
/// past 60min, so the default (5h) leaves headroom while still failing
/// with a diagnostic before the 6h platform cap. Overridable via
/// `DCS_CHECK_TIMEOUT_SECS` for harness-level fakes.
fn check_timeout() -> Duration {
    std::env::var("DCS_CHECK_TIMEOUT_SECS")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .map(Duration::from_secs)
        .unwrap_or_else(|| Duration::from_secs(5 * 3600))
}

/// Runs `cmd` to completion, killing it after `timeout`. `Ok` carries
/// the completed output (even on nonzero status); `Err` names the
/// timeout and what was running.
///
/// Output is captured to scratch files, never pipes: the poll loop
/// below cannot drain pipes while waiting, so a verbose child (the
/// nested `ci/check.sh` transcript is megabytes) would fill the pipe
/// buffer and block forever — the bound itself becoming the hang it
/// guards against. Files also free the reaper from grandchildren
/// holding inherited descriptors open.
fn run_bounded(cmd: &mut Command, timeout: Duration, what: &str) -> Result<Output, String> {
    let capture = std::env::temp_dir().join(format!(
        "dcs-bounded-run-{}-{:?}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&capture)
        .unwrap_or_else(|error| panic!("failed to create {what} capture dir: {error}"));
    let stdout_path = capture.join("stdout");
    let stderr_path = capture.join("stderr");
    cmd.stdout(
        std::fs::File::create(&stdout_path)
            .unwrap_or_else(|error| panic!("failed to capture {what} stdout: {error}")),
    )
    .stderr(
        std::fs::File::create(&stderr_path)
            .unwrap_or_else(|error| panic!("failed to capture {what} stderr: {error}")),
    );
    let mut child = cmd
        .spawn()
        .unwrap_or_else(|error| panic!("failed to spawn {what}: {error}"));
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let stdout = std::fs::read(&stdout_path).unwrap_or_default();
                let stderr = std::fs::read(&stderr_path).unwrap_or_default();
                let _ = std::fs::remove_dir_all(&capture);
                return Ok(Output {
                    status,
                    stdout,
                    stderr,
                });
            }
            Ok(None) => {
                if Instant::now() >= deadline {
                    // Kill and reap the direct child only: orphans keep
                    // their own file descriptors, so reaping never blocks
                    // on grandchildren the way draining pipes would.
                    let _ = child.kill();
                    let _ = child.wait();
                    let _ = std::fs::remove_dir_all(&capture);
                    return Err(format!("{what} timed out after {}s", timeout.as_secs()));
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(error) => panic!("failed waiting on {what}: {error}"),
        }
    }
}

/// One bounded `git fetch --depth 1 <source> <object>` into `remote_dir`.
/// Returns `true` only when the fetch completed with success; a stall
/// (or any transport failure) is `false` and names the source in the
/// returned note so the caller can report which remote stalled.
fn fetch_one(remote_dir: &Path, source: &str, object: &str, timeout: Duration) -> bool {
    let mut cmd = Command::new("git");
    cmd.args(["fetch", "--depth", "1", source, object])
        .current_dir(remote_dir);
    let what = format!("git fetch --depth 1 {source} {object}");
    match run_bounded(&mut cmd, timeout, &what) {
        Ok(output) => output.status.success(),
        Err(note) => {
            eprintln!("bounded fetch: {note}");
            false
        }
    }
}

/// Runs a nested `ci/check.sh` `cmd` to completion under the harness
/// bound: a stall inside the check (e.g. a hung `cargo fetch` against
/// an unreachable remote) fails naming the remote, never hangs the
/// merge-gated leg to the platform cap. `Ok` carries the completed
/// output unchanged so existing lockfile-stage diagnostics are
/// preserved.
fn run_check(cmd: &mut Command, remote: &str) -> Output {
    let timeout = check_timeout();
    let what = format!("ci/check.sh with DCS_REMOTE={remote}");
    match run_bounded(cmd, timeout, &what) {
        Ok(output) => output,
        Err(note) => panic!("{PIN_UNRESOLVABLE}: {note} (DCS_REMOTE={remote})"),
    }
}

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
/// `dcs-controller`, `dcs-plant-server`, `dcs-ctl`,
/// `dcs-alarm-report`, and the plant-side `dcs-plant-ctl` — are built
/// for the check's `DCS_TOOLS` substitution.
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
            "-p",
            "dcs-sim-net",
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

/// The release pin the template's manifest records for the release
/// crates — `tag = "<name>"` or `rev = "<sha>"` — the object the
/// stand-in remote must serve.
fn pinned_release(dir: &Path) -> String {
    let manifest = std::fs::read_to_string(dir.join("Cargo.toml")).unwrap();
    for line in manifest.lines() {
        for key in ["tag = \"", "rev = \""] {
            if let Some(start) = line.find(key) {
                let rest = &line[start + key.len()..];
                if let Some(end) = rest.find('"') {
                    return rest[..end].to_owned();
                }
            }
        }
    }
    panic!("the template's Cargo.toml records no release pin");
}

/// The release the template's deployment manifest declares — the
/// `docs/releases/<tag>/` record directory the check fetches its
/// record artifacts from.
fn declared_release(dir: &Path) -> String {
    let manifest: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(dir.join("deploy/manifest.json")).unwrap())
            .expect("deploy/manifest.json is JSON");
    manifest["dcs_release"]
        .as_str()
        .expect("deploy/manifest.json records no dcs_release")
        .to_owned()
}

/// The `DCS_UPGRADE_REV` default the tree's own `ci/check.sh` records —
/// the previous release's recorded rev the `upgrade` stage materializes
/// its baseline at.
fn recorded_upgrade_from(dir: &Path) -> String {
    let check = std::fs::read_to_string(dir.join("ci/check.sh")).unwrap();
    for line in check.lines() {
        if let Some(start) = line.find("DCS_UPGRADE_REV:-") {
            let rest = &line[start + "DCS_UPGRADE_REV:-".len()..];
            if let Some(end) = rest.find('}') {
                return rest[..end].to_owned();
            }
        }
    }
    panic!("the template's ci/check.sh records no DCS_UPGRADE_REV default");
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

/// The precise revision the template's committed `Cargo.lock` records
/// for the release crates — the commit the manifest's pin resolves to,
/// the one `--locked` fetches, and the one a fresh clone's committed
/// artifact must already name. Every `dcs-*` git source in the lockfile
/// must agree on it: the check's `lockfile` stage refuses a lockfile
/// whose release crates record more than one source.
fn committed_lock_rev(dir: &Path) -> String {
    let lock = std::fs::read_to_string(dir.join("Cargo.lock")).unwrap();
    let mut revisions: Vec<String> = Vec::new();
    for line in lock.lines() {
        let Some(source) = line.strip_prefix("source = \"git+") else {
            continue;
        };
        let Some((_, precise)) = source[..source.len() - 1].rsplit_once('#') else {
            continue;
        };
        if !revisions.iter().any(|seen| seen == precise) {
            revisions.push(precise.to_owned());
        }
    }
    assert_eq!(
        revisions.len(),
        1,
        "the template's Cargo.lock records no single precise revision for the release \
         crates: {revisions:?}"
    );
    let precise = &revisions[0];
    assert_eq!(
        precise.len(),
        40,
        "the recorded revision is not a commit sha"
    );
    precise.clone()
}

/// A `file://` stand-in for the published origin: a bare repository in
/// the materialized scratch that serves exactly the recorded pin. The
/// workspace checkout alone cannot play the remote in CI — its shallow
/// object store lacks the pinned commit and serves no way to name it —
/// so the stand-in is seeded with the objects the pin names. The seed
/// lands in the per-test bare repository, never the shared checkout's
/// store: the CI shards run these proofs as concurrent processes, and
/// two fetches into one repository collide on its lock files.
///
/// A `rev` pin names an existing commit and is fetched verbatim. A
/// `tag` pin names the release tag — which the published remote does not
/// serve until the supervisor cuts it — and the tag lands on the commit
/// the template's committed `Cargo.lock` records for it, which is the
/// release record's recorded `Commit` field. Seeding the tag there
/// rather than at a commit synthesized from the working tree is what
/// lets the committed lockfile take part in this proof at all: cargo
/// fetches the locked precise revision under `--locked`, so a stand-in
/// serving any other commit would force a re-resolve and the shipped
/// artifact would go unexercised exactly as it did before this seed
/// read the lockfile.
///
/// The `upgrade` stage's baseline — the previous release's recorded rev
/// the check's own `DCS_UPGRADE_REV` default names — is seeded beside it
/// so the crossing resolves.
fn serve_pinned_rev(scratch: &Path) -> String {
    let pin = pinned_release(scratch);
    let remote = scratch.join("dcs-remote.git");
    git(scratch, &["init", "--bare", "dcs-remote.git"]);
    // The local transport serves the object directly when the
    // checkout's store holds it; the published origin is the fallback
    // when the shallow store cannot. Each attempt is bounded so a
    // stalled remote fails naming the remote instead of hanging to
    // the platform's job cap.
    let timeout = fetch_timeout();
    let seed = |object: &str| {
        [root().display().to_string(), PUBLISHED_REMOTE.to_string()]
            .iter()
            .any(|source| fetch_one(&remote, source, object, timeout))
    };
    if pin.len() == 40 && pin.chars().all(|c| c.is_ascii_hexdigit()) {
        assert!(
            seed(&pin),
            "{PIN_UNRESOLVABLE}: no remote could serve the pinned rev {pin}"
        );
        git(&remote, &["update-ref", "refs/heads/main", &pin]);
    } else {
        let locked = committed_lock_rev(scratch);
        assert!(
            seed(&locked),
            "{PIN_UNRESOLVABLE}: no remote could serve the revision the committed \
             Cargo.lock records for {pin} — {locked}"
        );
        git(&remote, &["update-ref", "refs/heads/main", &locked]);
        git(
            &remote,
            &["update-ref", &format!("refs/tags/{pin}"), &locked],
        );
    }
    let upgrade_from = recorded_upgrade_from(scratch);
    assert!(
        seed(&upgrade_from),
        "{PIN_UNRESOLVABLE}: no remote could serve the upgrade-from rev {upgrade_from}"
    );
    git(
        &remote,
        &["update-ref", "refs/heads/upgrade", &upgrade_from],
    );
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
    /// `file://` stand-in — in the manifest *and*, with it, in the
    /// committed `Cargo.lock`. The release pin itself is left exactly as
    /// recorded: the same `rev`/`tag` fragment and the same resolved
    /// revision, so `cargo metadata --locked` resolves the copy against
    /// the stand-in without re-resolving, and the shipped lockfile's
    /// agreement with the shipped manifest is exercised rather than
    /// rewritten away. Rewriting only the manifest would make the two
    /// disagree on the remote, which is itself `lockfile-stale`.
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
        for name in ["Cargo.toml", "Cargo.lock"] {
            let manifest = dir.join(name);
            let source = std::fs::read_to_string(&manifest).unwrap();
            assert!(
                source.contains(PUBLISHED_REMOTE),
                "the template's {name} no longer records the published remote"
            );
            std::fs::write(&manifest, source.replace(PUBLISHED_REMOTE, &remote)).unwrap();
        }
        Self { dir, remote }
    }

    /// Runs the template's own clean-CI path against the `file://`
    /// stand-in remote and the locally built tooling — the same
    /// substitutions `consumer_release.rs` makes. `DCS_UPGRADE_REV`
    /// keeps the check's own recorded default — the previous release's
    /// rev — so the stage proves the real named crossing onto the pin's
    /// release; the stand-in serves both ends of it.
    fn check(&self, tools: &Path) -> Output {
        self.run(Some(tools), true)
    }

    /// The same run without the tooling substitution, for a check that
    /// must fail before its `tooling` stage resolves any binary.
    fn check_without_tooling(&self) -> Output {
        self.run(None, true)
    }

    /// The shipped configuration: the remote substitution a consumer's
    /// own CI makes and nothing else — never the workspace proof's
    /// `DCS_RECORD_DIR` record tree, so the release record the check
    /// reads is the one it fetches itself at the pinned rev.
    fn check_shipped(&self) -> Output {
        self.run(None, false)
    }

    fn run(&self, tools: Option<&Path>, record_dir: bool) -> Output {
        let mut check = Command::new("bash");
        check
            .arg("ci/check.sh")
            .current_dir(&self.dir)
            .env("DCS_REMOTE", &self.remote)
            .env("CARGO_TARGET_DIR", self.dir.join("target"));
        if record_dir {
            check.env("DCS_RECORD_DIR", root().join("docs/releases"));
        }
        if let Some(tools) = tools {
            check.env("DCS_TOOLS", tools);
        }
        run_check(&mut check, &self.remote)
    }
}

impl Drop for Materialized {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// Runs `cargo` in the materialized copy with an isolated target dir.
fn cargo_in(dir: &Path, args: &[&str]) -> Output {
    Command::new(CARGO)
        .args(args)
        .current_dir(dir)
        .env("CARGO_TARGET_DIR", dir.join("target"))
        .output()
        .unwrap_or_else(|error| panic!("cargo {args:?} failed to spawn: {error}"))
}

/// The version the template's committed lockfile records for a released
/// crate — the version a vendored path copy of that crate carries in
/// the path-dependency-leak reproduction, so cargo records both under
/// one name.
fn recorded_crate_version(dir: &Path, name: &str) -> String {
    let lock = std::fs::read_to_string(dir.join("Cargo.lock")).unwrap();
    let block = lock
        .split("[[package]]")
        .find(|block| block.contains(&format!("\nname = \"{name}\"\n")))
        .unwrap_or_else(|| panic!("{name} missing from the template's Cargo.lock"));
    block
        .lines()
        .find_map(|line| line.strip_prefix("version = \""))
        .and_then(|rest| rest.strip_suffix('"'))
        .unwrap_or_else(|| panic!("{name} records no version in the template's Cargo.lock"))
        .to_owned()
}

/// The full materialization proof: the template's own clean-CI check
/// passes green outside the workspace — a committed lockfile recording
/// the declared pin and resolving under `--locked`, byte-stable emit
/// matching the checked-in artifacts, released-tooling
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
    // The `lockfile` stage ran and held, before anything could
    // re-resolve: the committed lockfile records the manifest's own pin
    // at the stand-in's tag target, and the leg's doctored copy — the
    // release crates recorded at another revision — reported
    // `lockfile-stale` instead of passing.
    assert!(
        stdout.contains("== lockfile =="),
        "the lockfile stage did not run:\n{stdout}"
    );
    assert!(
        stdout.contains("a lockfile recorded at another revision refused: lockfile-stale"),
        "the lockfile stage's doctored case did not report its named diagnostic:\n{stdout}"
    );
    assert!(
        stdout.contains("a lockfile missing a release crate refused: lockfile-stale"),
        "the lockfile stage's missing-crate doctored case did not report its named diagnostic:\n{stdout}"
    );
    assert!(
        stdout.contains("a lockfile recording a release crate twice refused"),
        "the lockfile stage's duplicate-record doctored cases were not refused:\n{stdout}"
    );
    // The movable-pin half, the reported defect
    // (`lockfile-leg-rev-compare-skipped-for-non-full-sha`): every
    // spelling Cargo resolves afresh in `rev =` — the branch name, an
    // abbreviated sha, a tag name — is compared against what the
    // remote serves under that name, so a lockfile recording a
    // revision its pin has moved past is refused, and the same
    // spelling recording the revision the pin does name is accepted.
    // A leg refusing every movable pin would pass the refusal half, so
    // the accepted half is asserted beside it. Read the stage's own
    // section — the upgrade stage re-runs the whole pipeline, and its
    // repinned pass repeats the evidence.
    let lock_stage = stdout
        .split("== lockfile ==")
        .nth(1)
        .and_then(|tail| tail.split("== resolve ==").next())
        .unwrap_or_else(|| panic!("the lockfile stage did not run:\n{stdout}"));
    let movable: Vec<&str> = lock_stage
        .lines()
        .filter(|line| line.contains("a `rev = "))
        .collect();
    assert_eq!(
        movable.len(),
        6,
        "the lockfile stage did not check every movable `rev` spelling, twice each:\n\
         {lock_stage}"
    );
    assert_eq!(
        movable
            .iter()
            .filter(|line| line.contains("refused: lockfile-stale"))
            .count(),
        3,
        "the lockfile stage did not refuse every movable pin recording a revision its \
         pin has moved past:\n{lock_stage}"
    );
    assert_eq!(
        movable
            .iter()
            .filter(|line| line.contains("recording the revision the pin names accepted"))
            .count(),
        3,
        "the lockfile stage refused a movable `rev` pin recording the revision it names:\n\
         {lock_stage}"
    );
    let lock_line = stdout
        .lines()
        .find(|line| line.contains("the committed Cargo.lock records"))
        .unwrap_or_else(|| panic!("the lockfile stage reported no recorded pin:\n{stdout}"));
    assert!(
        lock_line.contains(&committed_lock_rev(&root().join("reference-plant"))),
        "the lockfile stage recorded no revision the committed lockfile carries: {lock_line}"
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
        "record's artifacts byte-identically",
        "a drifted record artifact refused: schema-drift",
        // The consumer-document screening legs ran and held: the
        // checked-in manifest and dynamics documents conformed to
        // their declared record artifacts, and each doctored
        // schema-violating copy reported schema-mismatch.
        "the deployment manifest conforms to the recorded schema artifact",
        "the dynamics document conforms to the recorded schema artifact",
        "manifest missing-required refused: schema-mismatch",
        "manifest mistyped-field refused: schema-mismatch",
        "manifest undeclared-field refused: schema-mismatch",
        "dynamics missing-required refused: schema-mismatch",
        "dynamics mistyped-field refused: schema-mismatch",
        "dynamics undeclared-element refused: schema-mismatch",
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
        // The alarm-validation leg ran and held: the emitted model's
        // managed-alarm record audited, the doctored copies refused
        // by the released `--check`, and the leg's own
        // skipped-doctoring negative case reported its diagnostic.
        "managed alarm instances carry the declared record",
        "doctored documents refused",
        "a skipped-doctoring run refused: alarm-validation",
        // The fingerprint stage's dynamics leg ran and held: the
        // manifest-declared pair served the declared dynamics.path,
        // its canonical fingerprint matched the recorded
        // dynamics.fingerprint and the checked-in artifact, and the
        // leg's own renumbered-points case reported its diagnostic.
        "dynamics fingerprint",
        "dynamics-fingerprint-digest",
        "renumber-points: reported, manifest-fingerprint-mismatch",
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
    // The checked-in manifest declares the deployed pair under
    // `topology.pairs`, and the deploy-stage cases each held: the
    // declaration validates under another pair name, while a member
    // the rig does not declare, a member two pairs share, a member
    // whose standby edge leaves the pair, a pair carrying two
    // standby declarations, or a declared pair whose standby wiring
    // does not close inside it each report the named
    // mismatch — and decision 99's one-field-per-deployment bound
    // refuses the planted undeployable shapes: a second declared pair
    // over the manifest's one plant and a second duty controller the
    // section never names, each a second claimant on the field's
    // single-writer claim.
    for line in [
        "topology-declared: optional declaration — the manifest and the rig agree",
        "topology-multi-pair refused: rig-mismatch",
        "undeployable-second-duty refused: rig-mismatch",
        "topology-undeclared-member refused: rig-mismatch",
        "topology-shared-member refused: rig-mismatch",
        "topology-external-standby refused: rig-mismatch",
        "topology-two-standbys refused: rig-mismatch",
        "topology-unwired-pair refused: rig-mismatch",
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
    // The pair contract's demote-boundary pending-command leg ran and
    // held: the receipted write admitted on the field owner and
    // demoted past inside its pending window settled exactly once —
    // the fenced image's first quiesced scan audited clean before the
    // promoted peer's first field-owning scan, both peers' journals,
    // receipt logs, images, and durable files carrying the single
    // audited settle — its digest line reports the evidence, and each
    // doctored expectation reported its named diagnostic.
    let demote_pending_line = stdout
        .lines()
        .find(|line| line.contains("demote-pending-digest"))
        .unwrap_or_else(|| panic!("the demote-pending leg reported no digest:\n{stdout}"));
    for phrase in [
        "demoted inside the pending window at tick",
        "fenced image audited at tick",
        "persisted settle records",
        "roles restored at tick",
    ] {
        assert!(
            demote_pending_line.contains(phrase),
            "the demote-pending digest names no '{phrase}' evidence: {demote_pending_line}"
        );
    }
    for tamper in ["phantom-applied", "unaudited-drop"] {
        assert!(
            stdout.contains(&format!("{tamper}: reported, demote-pending-failed")),
            "the demote-pending leg's {tamper} case did not report its named diagnostic:\n{stdout}"
        );
    }
    // The pair contract's demote-follow reconvergence leg ran and
    // held: under the manifest's declared 0.0.0.0 listen binds, both
    // documented switch directions left the demoted peer reconverged
    // to tracking on the successor's dialable announced source, the
    // tracking held across the pull train, and the launch roles
    // restored — its digest line reports the evidence, and the
    // self-addressed announce case reported its named diagnostic.
    let reconvergence_line = stdout
        .lines()
        .find(|line| line.contains("demote-reconvergence-digest"))
        .unwrap_or_else(|| panic!("the demote-reconvergence leg reported no digest:\n{stdout}"));
    for phrase in [
        "tracking its announced successor",
        "pulls",
        "roles restored",
    ] {
        assert!(
            reconvergence_line.contains(phrase),
            "the demote-reconvergence digest names no '{phrase}' evidence: {reconvergence_line}"
        );
    }
    assert!(
        stdout.contains("self-announce: reported, demote-reconvergence-failed"),
        "the demote-reconvergence leg's doctored case did not report its named diagnostic:\n{stdout}"
    );
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
    // The pair contract's emit-identical event-parity leg ran and
    // held: the counted step_completed set served identical routed
    // event records through both peers' GET /resources views while
    // the standby reported tracking — its digest line reports the
    // evidence — and each doctored case reported its named
    // diagnostic.
    let parity_line = stdout
        .lines()
        .find(|line| line.contains("event-parity-digest"))
        .unwrap_or_else(|| panic!("the event-parity leg reported no digest:\n{stdout}"));
    for phrase in [
        "counted step_completed records identical on both peers",
        "the standby tracking",
    ] {
        assert!(
            parity_line.contains(phrase),
            "the event-parity digest names no '{phrase}' evidence: {parity_line}"
        );
    }
    for tamper in ["dropped-event-record", "reattributed-event-record"] {
        assert!(
            stdout.contains(&format!("{tamper}: reported, event-parity-failed")),
            "the event-parity leg's {tamper} case did not report its named diagnostic:\n{stdout}"
        );
    }
    // The pair contract's commissioning/handover record leg ran and
    // held: the declared commissioning record materialized from one
    // deterministic driven run — its digest line reports each named
    // artifact's evidence — and every missing-artifact doctored case
    // reported its named diagnostic.
    let commissioning_line = stdout
        .lines()
        .find(|line| line.contains("commissioning-digest"))
        .unwrap_or_else(|| panic!("the commissioning leg reported no digest:\n{stdout}"));
    for phrase in [
        "field points checked out",
        "loop-check marks driven",
        "managed alarm instances signed off",
        "switched at tick",
        "restored at tick",
        "turnover documents digested",
    ] {
        assert!(
            commissioning_line.contains(phrase),
            "the commissioning digest names no '{phrase}' evidence: {commissioning_line}"
        );
    }
    for tamper in [
        "missing-io-checkout",
        "missing-loop-check",
        "missing-alarm-signoff",
        "missing-documentation-turnover",
    ] {
        assert!(
            stdout.contains(&format!("{tamper}: reported, commissioning-failed")),
            "the commissioning leg's {tamper} case did not report its named diagnostic:\n{stdout}"
        );
    }
    // The pair contract's rolling controller-upgrade leg ran and
    // held: the pair launched on the upgrade-from revision's
    // tooling (under the file:// stand-in the same substituted
    // binaries), each peer rolled onto the pinned binary one
    // process at a time resuming at its persisted tick, the
    // promoted peer's receipted command settled exactly once, and
    // the plant's step record named no unowned window — its digest
    // line reports the evidence, and the doctored expectation
    // reported its named diagnostic.
    let rolling_line = stdout
        .lines()
        .find(|line| line.contains("rolling-upgrade-digest"))
        .unwrap_or_else(|| panic!("the rolling-upgrade leg reported no digest:\n{stdout}"));
    for phrase in [
        "rolled and resumed at tick",
        "promoted at tick",
        "launch roles restored at tick",
        "owner scans",
    ] {
        assert!(
            rolling_line.contains(phrase),
            "the rolling-upgrade digest names no '{phrase}' evidence: {rolling_line}"
        );
    }
    assert!(
        stdout.contains("expect-degraded: reported, rolling-upgrade-failed"),
        "the rolling-upgrade leg's expect-degraded case did not report its named diagnostic:\n{stdout}"
    );
    // The pair contract's in-service revision-roll leg ran and held:
    // the consumer's own revision-2 composition emitted
    // deterministically and validated under the released tooling, the
    // `--revised` peer reported the named crossing with its carryover
    // report — the retained held point carried, the added point
    // initialized — the served fingerprint advanced, the switch
    // settled the revised peer `active` with control continuing, and
    // the incompatible revision drew the named refusal — its digest
    // line reports the evidence, and each doctored case reported its
    // named diagnostic.
    let revision_line = stdout
        .lines()
        .find(|line| line.contains("revision-roll-digest"))
        .unwrap_or_else(|| panic!("the revision-roll leg reported no digest:\n{stdout}"));
    for phrase in [
        "crossed to",
        "carried",
        "handover at tick",
        "incompatible refused",
    ] {
        assert!(
            revision_line.contains(phrase),
            "the revision-roll digest names no '{phrase}' evidence: {revision_line}"
        );
    }
    for tamper in ["unarmed-revision", "incompatible-model"] {
        assert!(
            stdout.contains(&format!("{tamper}: reported, revision-roll-failed")),
            "the revision-roll leg's {tamper} case did not report its named diagnostic:\n{stdout}"
        );
    }
    // The pair contract's configuration-backup and restore leg ran and
    // held: the running pair backed up beside the live deployment, the
    // completeness audit held the whole set, the wipe-and-restore onto
    // fresh volumes resumed each peer at its persisted tick with the
    // receipt log and journal order continuing, and the rejoined pair
    // reconverged — its digest line reports the evidence, and each
    // missing-artifact doctored case reported its named diagnostic.
    let backup_line = stdout
        .lines()
        .find(|line| line.contains("backup-restore-digest"))
        .unwrap_or_else(|| panic!("the backup-restore leg reported no digest:\n{stdout}"));
    for phrase in [
        "backed up",
        "resumed duty at tick",
        "standby at tick",
        "run continued to tick",
    ] {
        assert!(
            backup_line.contains(phrase),
            "the backup-restore digest names no '{phrase}' evidence: {backup_line}"
        );
    }
    for tamper in ["missing-state-file", "missing-journal-file"] {
        assert!(
            stdout.contains(&format!("{tamper}: reported, backup-restore-failed")),
            "the backup-restore leg's {tamper} case did not report its named diagnostic:\n{stdout}"
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

/// The shipped consumer artifact resolves reproducibly, or the
/// contract says so by name. `reference-plant/Cargo.lock` is committed
/// so every build resolves the same sources (README §2), and the pin it
/// records is the release record's recorded commit — so a fresh clone
/// must resolve under `--locked` without a resolver repairing the file
/// first. This materializes the tree with that lockfile intact and the
/// manifest's own `tag` pin against the `file://` stand-in, which
/// serves the tag at the very revision the lockfile records, and
/// asserts `cargo metadata --locked` succeeds on the copy and leaves
/// the committed lockfile byte-identical.
///
/// The structural half of the same gap: a lockfile recorded at another
/// revision must be named, not absorbed. The check's `lockfile` stage
/// runs before `resolve`, so the doctored copy is refused at once —
/// this is the reported defect (`consumer-lockfile-stale-vs-declared-
/// pin`: a `rev`-recorded lockfile against a `tag` pin, invisible to a
/// consumer's CI because the resolve fallback rewrote the file before
/// anything read it).
#[test]
fn the_committed_lockfile_satisfies_the_declared_pin() {
    let copy = Materialized::new();
    let lock = copy.dir.join("Cargo.lock");
    let before = std::fs::read(&lock).unwrap();

    let output = Command::new(CARGO)
        .args(["metadata", "--locked", "--format-version", "1"])
        .current_dir(&copy.dir)
        .env("CARGO_TARGET_DIR", copy.dir.join("target"))
        .output()
        .expect("cargo metadata runs");
    assert!(
        output.status.success(),
        "the committed Cargo.lock does not satisfy the declared pin: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        std::fs::read(&lock).unwrap(),
        before,
        "the locked resolve rewrote the committed Cargo.lock"
    );

    // The reported defect, put back: the release crates recorded at
    // another revision's `rev` pin while the manifest declares this
    // tree's own tag pin.
    let pin = pinned_release(&copy.dir);
    let precise = committed_lock_rev(&copy.dir);
    let baseline = recorded_upgrade_from(&copy.dir);
    let committed = std::fs::read_to_string(&lock).unwrap();
    let stale = committed.replace(
        &format!("?tag={pin}#{precise}"),
        &format!("?rev={baseline}#{baseline}"),
    );
    assert_ne!(
        stale, committed,
        "the committed lockfile records no `?tag={pin}#{precise}` source to doctor"
    );
    std::fs::write(&lock, &stale).unwrap();
    let mut refused_cmd = Command::new("bash");
    refused_cmd
        .arg("ci/check.sh")
        .current_dir(&copy.dir)
        .env("DCS_REMOTE", &copy.remote)
        .env("DCS_RECORD_DIR", root().join("docs/releases"))
        .env("CARGO_TARGET_DIR", copy.dir.join("target"));
    let refused = run_check(&mut refused_cmd, &copy.remote);
    let stderr = String::from_utf8_lossy(&refused.stderr);
    assert!(
        !refused.status.success(),
        "a Cargo.lock recorded at another revision passed the check"
    );
    assert!(
        stderr.contains("lockfile-stale"),
        "a stale committed lockfile was refused without its named diagnostic:\n{stderr}"
    );
    assert!(
        !String::from_utf8_lossy(&refused.stdout).contains("== resolve =="),
        "the stale lockfile was caught only after the resolve stage re-resolved it:\n{stderr}"
    );
    assert_eq!(
        std::fs::read_to_string(&lock).unwrap(),
        stale,
        "the refused run repaired the doctored lockfile instead of reporting it"
    );
}

/// A release tag re-pointed after the cut is the tamper the record's
/// `Commit` field exists to detect: a lockfile regenerated to follow
/// the moved tag satisfies every leg the tag's own target answers —
/// the manifest's `tag` query still matches, and the remote's served
/// target still equals the recorded revision — so only the record
/// still naming the commit the release was cut on calls the
/// divergence out. The reported defect
/// (`lockfile-record-commit-check-unreachable-in-consumer-path`):
/// that comparison was wired only into the workspace proof's
/// `DCS_RECORD_DIR` substitution, which a consumer running the
/// template's own CI never makes — the shipped check fetched the
/// record tree for its schema artifacts and never fed `record.md` to
/// the leg.
///
/// The reproduction is the reported one: the stand-in's tag is
/// re-pointed at a commit whose release record still names the tag's
/// original target in its `Commit` field — the record as a post-cut
/// descendant carries it — the committed lockfile is rewritten to
/// record the new target exactly as `cargo update` would have, and
/// the shipped configuration — `DCS_REMOTE` and nothing else — must
/// report `lockfile-stale` naming the record's commit.
#[test]
fn a_repointed_release_tag_reports_lockfile_stale() {
    let copy = Materialized::new();
    let pin = pinned_release(&copy.dir);
    let precise = committed_lock_rev(&copy.dir);

    // The moved tag's target: a child of the tag's original target
    // whose only change fills the release record's `Commit` field with
    // that original sha — so the crate trees the lockfile resolves
    // stay the original commit's, and every leg but the record's
    // Commit-field comparison still answers green.
    git(
        &copy.dir,
        &[
            "clone",
            "--quiet",
            "--branch",
            "main",
            &copy.remote,
            "moved-tag",
        ],
    );
    let work = copy.dir.join("moved-tag");
    let record_path = work
        .join("docs/releases")
        .join(declared_release(&copy.dir))
        .join("record.md");
    let contents = std::fs::read_to_string(&record_path).unwrap();
    let filled = if contents.contains(&format!("| Commit | `{precise}`")) {
        contents
    } else {
        let edited = contents.replacen(
            "| Commit | *pending*",
            &format!("| Commit | `{precise}`"),
            1,
        );
        assert_ne!(
            edited, contents,
            "the record's Commit field is neither `{precise}` nor a pending \
             field this test can fill"
        );
        edited
    };
    std::fs::write(&record_path, &filled).unwrap();
    git(&work, &["add", "-A"]);
    git(
        &work,
        &[
            "-c",
            "user.name=dcs-ci",
            "-c",
            "user.email=dcs-ci@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "re-point the release record",
        ],
    );
    let moved = String::from_utf8(
        Command::new("git")
            .args(["rev-parse", "HEAD"])
            .current_dir(&work)
            .output()
            .expect("git rev-parse runs")
            .stdout,
    )
    .expect("git rev-parse answers utf8")
    .trim()
    .to_owned();
    git(
        &work,
        &[
            "push",
            "--quiet",
            "--force",
            "origin",
            &format!("HEAD:refs/tags/{pin}"),
        ],
    );

    // The lockfile regenerated to follow the moved tag: every release
    // crate's record now names the tag's new target, exactly what
    // `cargo update` against the re-pointed ref would write.
    let lock = copy.dir.join("Cargo.lock");
    let committed = std::fs::read_to_string(&lock).unwrap();
    let regenerated = committed.replace(
        &format!("?tag={pin}#{precise}"),
        &format!("?tag={pin}#{moved}"),
    );
    assert_ne!(
        regenerated, committed,
        "the committed lockfile records no `?tag={pin}#{precise}` source to regenerate"
    );
    std::fs::write(&lock, &regenerated).unwrap();

    let refused = copy.check_shipped();
    let stdout = String::from_utf8_lossy(&refused.stdout);
    let stderr = String::from_utf8_lossy(&refused.stderr);
    assert!(
        !refused.status.success(),
        "a Cargo.lock regenerated to a re-pointed release tag passed the \
         shipped check:\n{stdout}"
    );
    assert!(
        stderr.contains("lockfile-stale"),
        "the re-pointed tag was refused without its named diagnostic:\n{stderr}"
    );
    assert!(
        stderr.contains(&format!("records commit {precise}")),
        "the refusal did not name the release record's Commit field as the \
         divergence:\n{stderr}"
    );
    assert!(
        !stdout.contains("== resolve =="),
        "the record-commit divergence was caught only after the resolve stage:\n{stdout}"
    );
    assert_eq!(
        std::fs::read_to_string(&lock).unwrap(),
        regenerated,
        "the refused run repaired the regenerated lockfile instead of reporting it"
    );
}

/// A tag query the remote cannot answer is unverifiable, never the
/// absent-tag skip — the reported defect
/// (`lockfile-leg-tag-check-skipped-on-ls-remote-failure`): the leg
/// parsed `git ls-remote`'s stdout only, so any transport failure —
/// a safe.directory refusal, an auth rejection, a corrupt repository —
/// produced an empty ref map indistinguishable from "tag not
/// published yet", and a lockfile recording a commit the tag does not
/// land on passed the leg while `cargo fetch --locked` stayed green
/// through libgit2 and cargo's git cache. The leg's verdict hung on
/// ambient git-CLI health rather than the artifact.
///
/// The reproduction: the stand-in serves the doctored record's sha —
/// a child of the tag's target advertised on its own branch, so the
/// tag still lands where the committed revision names it — while a
/// `git` shim on `PATH` refuses only `ls-remote`, the same
/// libgit2-vs-CLI divergence the rig's safe.directory refusal
/// produced. `cargo fetch --locked` still resolves the doctored
/// tree, but the leg refuses the unanswerable query, the shipped
/// check names `pin-unresolvable` before its resolve stage, and the
/// same artifact under a healthy CLI reports `lockfile-stale` naming
/// the tag's real target — the served-target comparison the
/// stand-in's tag seeding at the recorded commit leaves dead.
#[test]
fn an_unanswerable_tag_query_is_unverifiable_not_absent() {
    let copy = Materialized::new();
    let pin = pinned_release(&copy.dir);
    let precise = committed_lock_rev(&copy.dir);

    // A commit the remote serves that the tag does not land on: a
    // child of the tag's own target, so the crate trees the lockfile
    // resolves carry the recorded versions and `cargo fetch --locked`
    // answers green on the wrong sha exactly as the rig's did.
    git(
        &copy.dir,
        &[
            "clone",
            "--quiet",
            "--branch",
            "main",
            &copy.remote,
            "wrong-target",
        ],
    );
    let work = copy.dir.join("wrong-target");
    std::fs::write(work.join("wrong-target"), "not the tag's target\n").unwrap();
    git(&work, &["add", "-A"]);
    git(
        &work,
        &[
            "-c",
            "user.name=dcs-ci",
            "-c",
            "user.email=dcs-ci@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "a commit the tag does not land on",
        ],
    );
    let wrong = String::from_utf8(
        Command::new("git")
            .args(["rev-parse", "HEAD"])
            .current_dir(&work)
            .output()
            .expect("git rev-parse runs")
            .stdout,
    )
    .expect("git rev-parse answers utf8")
    .trim()
    .to_owned();
    git(
        &work,
        &["push", "--quiet", "origin", "HEAD:refs/heads/wrong-target"],
    );

    // The lockfile regenerated to record the wrong commit under the
    // tag's own query — the served target still the committed
    // revision, so only the leg's ls-remote comparison can name it.
    let lock = copy.dir.join("Cargo.lock");
    let committed = std::fs::read_to_string(&lock).unwrap();
    let doctored = committed.replace(
        &format!("?tag={pin}#{precise}"),
        &format!("?tag={pin}#{wrong}"),
    );
    assert_ne!(
        doctored, committed,
        "the committed lockfile records no `?tag={pin}#{precise}` source to doctor"
    );
    std::fs::write(&lock, &doctored).unwrap();

    // A `git` on PATH that refuses only `ls-remote` — the reported
    // reproduction's safe.directory divergence: the CLI cannot read
    // the remote while cargo's libgit2 still resolves it. Every
    // other verb delegates to the real binary resolved before the
    // shim enters PATH.
    let real_git = String::from_utf8(
        Command::new("sh")
            .args(["-c", "command -v git"])
            .output()
            .expect("sh runs")
            .stdout,
    )
    .expect("command -v git answers utf8")
    .trim()
    .to_owned();
    let shim_dir = copy.dir.join("shim-bin");
    std::fs::create_dir(&shim_dir).unwrap();
    let shim = shim_dir.join("git");
    std::fs::write(
        &shim,
        format!(
            "#!/bin/sh\n\
             if [ \"$1\" = ls-remote ]; then\n\
             echo 'fatal: detected dubious ownership in repository' >&2\n\
             exit 128\n\
             fi\n\
             exec {real_git} \"$@\"\n"
        ),
    )
    .unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&shim, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    let path = format!(
        "{}:{}",
        shim_dir.display(),
        std::env::var("PATH").expect("PATH is set")
    );

    // The green half of the reported reproduction: `cargo fetch
    // --locked` resolves the recorded sha without re-checking the
    // tag's target — only the leg could name this artifact.
    let fetched = Command::new(CARGO)
        .args(["fetch", "--locked"])
        .current_dir(&copy.dir)
        .env("CARGO_TARGET_DIR", copy.dir.join("target"))
        .env("PATH", &path)
        .output()
        .expect("cargo fetch runs");
    assert!(
        fetched.status.success(),
        "cargo fetch --locked did not resolve the wrong-sha lockfile — the \
         reproduction needs the fetch green while the leg refuses:\n{}",
        String::from_utf8_lossy(&fetched.stderr)
    );

    // Under the refusing CLI the leg must not pass: the tag's target
    // is unverifiable, which is not the absent-tag skip.
    let leg = Command::new("python3")
        .args(["ci/lockfile.py", "Cargo.lock", &copy.remote])
        .current_dir(&copy.dir)
        .env("PATH", &path)
        .output()
        .expect("the lockfile leg runs");
    assert!(
        !leg.status.success(),
        "the leg passed a tag query the remote could not answer"
    );
    let stderr = String::from_utf8_lossy(&leg.stderr);
    assert!(
        stderr.contains("ls-remote"),
        "the unanswerable query was refused without naming the failed query:\n{stderr}"
    );

    // Through the shipped check the same state names its diagnostic
    // before the resolve stage can rewrite the artifact.
    let mut refused_cmd = Command::new("bash");
    refused_cmd
        .arg("ci/check.sh")
        .current_dir(&copy.dir)
        .env("DCS_REMOTE", &copy.remote)
        .env("CARGO_TARGET_DIR", copy.dir.join("target"))
        .env("PATH", &path);
    let refused = run_check(&mut refused_cmd, &copy.remote);
    let stdout = String::from_utf8_lossy(&refused.stdout);
    let stderr = String::from_utf8_lossy(&refused.stderr);
    assert!(
        !refused.status.success(),
        "an unverifiable tag target passed the shipped check:\n{stdout}"
    );
    assert!(
        stderr.contains("pin-unresolvable"),
        "the unanswerable query was refused without its named diagnostic:\n{stderr}"
    );
    assert!(
        !stdout.contains("== resolve =="),
        "the unverifiable remote was caught only after the resolve stage:\n{stdout}"
    );
    assert_eq!(
        std::fs::read_to_string(&lock).unwrap(),
        doctored,
        "the refused run repaired the doctored lockfile instead of reporting it"
    );

    // The same artifact under a healthy CLI: the leg reaches the
    // served-target comparison and names the tag's real target.
    let leg = Command::new("python3")
        .args(["ci/lockfile.py", "Cargo.lock", &copy.remote])
        .current_dir(&copy.dir)
        .output()
        .expect("the lockfile leg runs");
    assert!(
        !leg.status.success(),
        "the leg passed a lockfile recording a commit the tag does not land on"
    );
    let stderr = String::from_utf8_lossy(&leg.stderr);
    assert!(
        stderr.contains(&format!("but {pin} lands on {precise}")),
        "the wrong-target record was refused without naming the tag's target:\n{stderr}"
    );
}

/// A stalled network fetch fallback fails fast naming the remote,
/// never hanging the merge-gated leg: the harness bounds both the
/// `git fetch --depth 1 <remote> <sha>` seed fallback and the nested
/// `ci/check.sh` run. This test uses harness-level fakes — an
/// unroutable remote and a hanging command — at short bounds beside
/// the positive foreign-pin leg below.
#[test]
fn a_stalled_fetch_fallback_fails_fast_naming_the_remote() {
    use std::time::Instant;
    // An unreachable remote on a closed loopback port: the transport
    // refuses immediately, so the fallback returns without touching the
    // network, while the hanging-command legs below prove the explicit
    // bound kills a genuine stall. (A documentation-routable address
    // would exercise git's own minute-scale TCP timeout instead.)
    let unreachable = "http://127.0.0.1:9/unreachable/dcs.git";
    let object = "0".repeat(40);
    let dir = std::env::temp_dir().join(format!(
        "dcs-bounded-fetch-{}-{:?}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    git(&dir, &["init", "--bare", "probe.git"]);
    let probe = dir.join("probe.git");
    let bound = Duration::from_secs(5);
    let started = Instant::now();
    let served = fetch_one(&probe, unreachable, &object, bound);
    let elapsed = started.elapsed();
    let _ = std::fs::remove_dir_all(&dir);
    assert!(
        !served,
        "a stalled remote {unreachable} was reported as served"
    );
    assert!(
        elapsed < bound + Duration::from_secs(30),
        "the stalled fetch did not fail fast: took {elapsed:?} against a {bound:?} bound"
    );
    // The bound names the remote in its diagnostic: the same `what`
    // string the seed fallback logs on a stall.
    let mut hanging = Command::new("sleep");
    hanging.arg("60");
    let what = format!("git fetch --depth 1 {unreachable} {object}");
    let timed_out = run_bounded(&mut hanging, Duration::from_secs(2), &what);
    let Err(note) = timed_out else {
        panic!("a hanging fetch survived its bound");
    };
    assert!(
        note.contains(unreachable),
        "the stall diagnostic did not name the remote:\n{note}"
    );
    // Equivalent bound on the nested check subprocess: a hanging child
    // is killed at the bound instead of waiting unbounded.
    let mut stalled_check = Command::new("sleep");
    stalled_check.arg("60");
    let started = Instant::now();
    let timed_out = run_bounded(
        &mut stalled_check,
        Duration::from_secs(2),
        "ci/check.sh with DCS_REMOTE=stalled-remote",
    );
    assert!(
        timed_out.is_err_and(|note| note.contains("stalled-remote")),
        "the nested check bound did not name the remote"
    );
    assert!(
        started.elapsed() < Duration::from_secs(30),
        "the nested check bound did not fail fast"
    );
    // A verbose child must still complete: the bound polls without
    // draining pipes, so piped capture would deadlock once a transcript
    // like the nested check's (megabytes) fills the pipe buffer — the
    // bound itself becoming the hang. `seq` emits ~1.4MB here, far past
    // any pipe buffer, and must arrive intact.
    let mut verbose = Command::new("seq");
    verbose.args(["1", "200000"]);
    let completed = run_bounded(&mut verbose, Duration::from_secs(60), "verbose child");
    let Ok(output) = completed else {
        panic!("a verbose child did not survive the bound");
    };
    assert!(
        output.status.success(),
        "a verbose child failed under the bound"
    );
    assert!(
        output.stdout.ends_with(b"200000\n"),
        "a verbose child's transcript was truncated under the bound"
    );
}

/// A consumer lockfile that legitimately carries a further git-pinned
/// package must still pass the `lockfile` stage end to end: the
/// positive leg judges only the release crates' records, and the
/// stage's self-check doctors only their `?query#sha` sources — never
/// a whole-file census of git sources. This is the reported defect
/// (`lockfile-doctor-hardcoded-git-source-count`): the doctor rewrote
/// every `?query#sha` source in the file and hard-aborted on any count
/// but three, so a consumer extending the template with a pinned git
/// dependency — or a lockfile recording a second version of a release
/// crate — crashed the stage with an opaque `doctor:` message and a
/// real stale or duplicate lockfile state never reached its named
/// diagnostic.
///
/// The materialized tree gains a `vendored-widget` dependency pinned
/// `tag = "v0.1.0"` against its own `file://` remote — a tiny crate
/// repository built in the scratch, tagged, and recorded in the
/// committed lockfile exactly as `cargo update` would have written it
/// — then runs the full clean check: the positive leg accepts the
/// recorded release pin beside the foreign source, and the doctored
/// copy still reports `lockfile-stale`.
#[test]
fn a_foreign_git_pin_beside_the_release_pins_passes_the_lockfile_stage() {
    let tools = build_tools();
    let copy = Materialized::new();

    // The vendored crate the template does not ship: a tiny library in
    // its own repository inside the materialized scratch — swept with
    // it — committed and tagged like a real consumer's pinned git
    // dependency.
    let vendored = copy.dir.join("vendored-widget");
    std::fs::create_dir_all(vendored.join("src")).unwrap();
    std::fs::write(
        vendored.join("Cargo.toml"),
        "[package]\nname = \"vendored-widget\"\nversion = \"0.1.0\"\nedition = \"2021\"\n",
    )
    .unwrap();
    std::fs::write(vendored.join("src/lib.rs"), "pub fn vendored() {}\n").unwrap();
    git(&vendored, &["init"]);
    git(&vendored, &["add", "-A"]);
    git(
        &vendored,
        &[
            "-c",
            "user.name=dcs-ci",
            "-c",
            "user.email=dcs-ci@example.invalid",
            "commit",
            "-m",
            "vendored widget",
        ],
    );
    git(&vendored, &["tag", "v0.1.0"]);
    let sha = String::from_utf8(
        Command::new("git")
            .args(["rev-parse", "HEAD"])
            .current_dir(&vendored)
            .output()
            .expect("git rev-parse runs")
            .stdout,
    )
    .expect("git rev-parse answers utf8");
    let remote = format!("file://{}", vendored.display());

    // The manifest declares the pin beside the release crates, and the
    // committed lockfile records it — the root package's dependency
    // list and a name-sorted `[[package]]` entry, the bytes
    // `cargo update` would have written for the added dependency.
    let manifest_path = copy.dir.join("Cargo.toml");
    let manifest = std::fs::read_to_string(&manifest_path).unwrap();
    let edited = manifest.replace(
        "serde_json = \"1\"",
        &format!(
            "serde_json = \"1\"\nvendored-widget = {{ git = \"{remote}\", tag = \"v0.1.0\" }}"
        ),
    );
    assert_ne!(
        edited, manifest,
        "the template's Cargo.toml no longer records the serde_json dependency"
    );
    std::fs::write(&manifest_path, edited).unwrap();

    let lock_path = copy.dir.join("Cargo.lock");
    let mut lock = std::fs::read_to_string(&lock_path).unwrap();
    let root_at = lock
        .find("name = \"pump-station\"")
        .expect("the template lockfile records no pump-station package");
    let deps_end = lock[root_at..]
        .find("\n]")
        .map(|at| root_at + at + 1)
        .expect("the pump-station package records no dependency list");
    lock.insert_str(deps_end, " \"vendored-widget\",\n");
    let zmij_at = lock
        .find("[[package]]\nname = \"zmij\"")
        .expect("the template lockfile no longer records the zmij package");
    lock.insert_str(
        zmij_at,
        &format!(
            "[[package]]\nname = \"vendored-widget\"\nversion = \"0.1.0\"\nsource = \"git+{remote}?tag=v0.1.0#{}\"\n\n",
            sha.trim()
        ),
    );
    std::fs::write(&lock_path, &lock).unwrap();
    // The reproduction shape: four git-pinned sources in the file —
    // the three release crates' and the consumer's own — where the
    // doctor's census used to abort.
    assert_eq!(
        lock.matches("?tag=").count(),
        4,
        "the doctored fixture does not carry a fourth git-pinned source"
    );

    let output = copy.check(&tools);
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        output.status.success(),
        "a consumer lockfile carrying an extra git-pinned package failed \
         the template's ci/check.sh:\nstdout:\n{stdout}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        stdout.contains("== lockfile =="),
        "the lockfile stage did not run:\n{stdout}"
    );
    assert!(
        stdout.contains("a lockfile recorded at another revision refused: lockfile-stale"),
        "the lockfile stage's doctored case did not report its named diagnostic \
         beside the foreign pin:\n{stdout}"
    );
}

/// A released crate reaching the build through a path package rather
/// than the pinned remote is `path-dependency-leak`, whatever
/// `cargo fetch --locked` says about the same file.
///
/// The reported defect (`lockfile-leg-misses-sourceless-path-package`):
/// the `lockfile` stage collected package records through a regex
/// requiring a `source` line directly after the version, and Cargo
/// writes a path package with no `source` key at all — so the record
/// never entered the collected map, the git-sourced record of the same
/// crate stood in for it, and the whole lockfile check ran green over a
/// lockfile that builds a released crate from a checkout. The
/// reproduction is the reported one: the materialized consumer adds a
/// crate of its own whose dependency on a released crate is a `path`
/// into a vendored copy — a legitimate customer action — the lockfile
/// is re-resolved, and the check must name the leak before the resolve
/// stage can re-resolve the artifact away.
///
/// The same blind spot's other face: a second record of a released
/// crate — from a registry rather than the pin — is the same finding,
/// but the per-name map kept only the last record under a name, so a
/// duplicate sorted ahead of the pinned one went unexamined. Every
/// record must be examined.
#[test]
fn a_path_sourced_released_crate_reports_path_dependency_leak() {
    let copy = Materialized::new();
    let lock = copy.dir.join("Cargo.lock");

    // The consumer's own crate, and the vendored copy of a released
    // crate it reaches by path — at the version the committed lockfile
    // records for that crate, so cargo records both under one name.
    let version = recorded_crate_version(&copy.dir, "dcs-model");
    let vendor = copy.dir.join("vendor");
    std::fs::create_dir_all(vendor.join("dcs-model/src")).unwrap();
    std::fs::create_dir_all(vendor.join("my-lib/src")).unwrap();
    std::fs::write(
        vendor.join("dcs-model/Cargo.toml"),
        format!(
            "\
[package]
name = \"dcs-model\"
version = \"{version}\"
edition = \"2021\"
"
        ),
    )
    .unwrap();
    std::fs::write(
        vendor.join("dcs-model/src/lib.rs"),
        "pub fn vendored() {}\n",
    )
    .unwrap();
    std::fs::write(
        vendor.join("my-lib/Cargo.toml"),
        "\
[package]
name = \"my-lib\"
version = \"0.1.0\"
edition = \"2021\"

[dependencies]
dcs-model = { path = \"../dcs-model\" }
",
    )
    .unwrap();
    std::fs::write(vendor.join("my-lib/src/lib.rs"), "pub fn lib() {}\n").unwrap();
    let manifest = copy.dir.join("Cargo.toml");
    let source = std::fs::read_to_string(&manifest).unwrap();
    let anchor = "serde_json = \"1\"\n";
    assert!(
        source.contains(anchor),
        "the template's dependency anchor moved: {source}"
    );
    std::fs::write(
        &manifest,
        source.replace(
            anchor,
            &format!("{anchor}my-lib = {{ path = \"vendor/my-lib\" }}\n"),
        ),
    )
    .unwrap();

    // Re-resolve: the record under test only exists once cargo has
    // written it beside the pinned one.
    let fetched = cargo_in(&copy.dir, &["fetch"]);
    assert!(
        fetched.status.success(),
        "the vendored path dependency did not resolve: {}",
        String::from_utf8_lossy(&fetched.stderr)
    );
    let injected = std::fs::read_to_string(&lock).unwrap();
    let sourceless: Vec<&str> = injected
        .split("[[package]]")
        .filter(|block| block.contains("\nname = \"dcs-model\"\n") && !block.contains("source = "))
        .collect();
    assert_eq!(
        sourceless.len(),
        1,
        "the reproduction recorded no sourceless dcs-model package block:\n{injected}"
    );
    assert!(
        injected.contains(&format!(
            "\nname = \"dcs-model\"\nversion = \"{version}\"\nsource = \"git+"
        )),
        "the pinned dcs-model record went missing:\n{injected}"
    );
    // The resolve stage's own fast path accepts this file — the defect
    // is invisible to it, which is why the lockfile stage must hold it
    // on its own.
    let locked = cargo_in(&copy.dir, &["fetch", "--locked"]);
    assert!(
        locked.status.success(),
        "cargo fetch --locked refused the injected lockfile: {}",
        String::from_utf8_lossy(&locked.stderr)
    );

    let refused = copy.check_without_tooling();
    let stdout = String::from_utf8_lossy(&refused.stdout);
    let stderr = String::from_utf8_lossy(&refused.stderr);
    assert!(
        !refused.status.success(),
        "a lockfile building a released crate through a path package passed the check:\n{stdout}"
    );
    assert!(
        stderr.contains("path-dependency-leak"),
        "a path-sourced released crate was refused without its named diagnostic:\n{stderr}"
    );
    assert!(
        !stdout.contains("== resolve =="),
        "the path leak was caught only after the resolve stage:\n{stdout}"
    );
    assert_eq!(
        std::fs::read_to_string(&lock).unwrap(),
        injected,
        "the refused run repaired the injected lockfile instead of reporting it"
    );

    // The other face of the same blind spot: a second record of the
    // released crate, this time one Cargo did write a `source` line
    // for — but at a registry rather than the pinned remote. It is the
    // same finding (a released crate off the git pin), and only the
    // last record under the name used to be examined, so a duplicate
    // sorted ahead of the pinned one went unexamined.
    let registry = "registry+https://github.com/rust-lang/crates.io-index";
    let version_line = format!("version = \"{version}\"\n");
    let doctored: String = injected
        .split("[[package]]")
        .enumerate()
        .map(|(index, block)| {
            if index > 0
                && !block.contains("source = ")
                && block.contains(&format!("\nname = \"dcs-model\"\n{version_line}"))
            {
                format!("[[package]]{}\nsource = \"{registry}\"\n", block.trim_end())
            } else if index > 0 {
                format!("[[package]]{block}")
            } else {
                block.to_owned()
            }
        })
        .collect();
    assert_ne!(
        doctored, injected,
        "the injected lockfile records no sourceless dcs-model block to doctor"
    );
    std::fs::write(&lock, &doctored).unwrap();
    let refused = copy.check_without_tooling();
    let stderr = String::from_utf8_lossy(&refused.stderr);
    assert!(
        !refused.status.success(),
        "a released crate recorded at a registry source passed the check"
    );
    assert!(
        stderr.contains("path-dependency-leak"),
        "a duplicate non-git record of a released crate was refused without its named \
         diagnostic:\n{stderr}"
    );
    assert_eq!(
        std::fs::read_to_string(&lock).unwrap(),
        doctored,
        "the refused run repaired the doctored lockfile instead of reporting it"
    );
}

/// The contract's other `lockfile-stale` case — the reported
/// `lockfile-missing-crate-reported-as-path-leak` defect: a release
/// crate whose `[[package]]` block is absent from the committed
/// lockfile entirely is stale, not `path-dependency-leak` — nothing is
/// recorded, from a path source or otherwise, while the leak diagnostic
/// names a crate *recorded* from a `path` source or with no source at
/// all. The materialization drops `dcs-core`'s package block — the
/// reported reproduction — and asserts the `lockfile` stage names the
/// stale diagnostic before `resolve` can repair the file.
#[test]
fn a_lockfile_missing_a_release_crate_reports_lockfile_stale() {
    let copy = Materialized::new();
    let lock = copy.dir.join("Cargo.lock");
    let committed = std::fs::read_to_string(&lock).unwrap();
    let start = committed
        .find("[[package]]\nname = \"dcs-core\"\n")
        .expect("the committed lockfile records a dcs-core package block");
    let end = committed[start..]
        .find("\n[[package]]")
        .map(|i| start + i + 1)
        .unwrap_or(committed.len());
    let doctored = format!("{}{}", &committed[..start], &committed[end..]);
    assert!(
        !doctored.contains("name = \"dcs-core\""),
        "the doctor left a dcs-core package block in the lockfile"
    );
    std::fs::write(&lock, &doctored).unwrap();

    let refused = copy.check_without_tooling();
    let stderr = String::from_utf8_lossy(&refused.stderr);
    assert!(
        !refused.status.success(),
        "a Cargo.lock missing a release crate passed the check"
    );
    assert!(
        stderr.contains("lockfile-stale"),
        "a lockfile missing a release crate was refused without its named diagnostic:\n{stderr}"
    );
    assert!(
        !stderr.contains("path-dependency-leak"),
        "a lockfile missing a release crate reported the leak diagnostic — \
         nothing is recorded, let alone a path source:\n{stderr}"
    );
    assert!(
        !String::from_utf8_lossy(&refused.stdout).contains("== resolve =="),
        "the incomplete lockfile was caught only after the resolve stage re-resolved it:\n{stderr}"
    );
    assert_eq!(
        std::fs::read_to_string(&lock).unwrap(),
        doctored,
        "the refused run repaired the doctored lockfile instead of reporting it"
    );
}

/// Every `[[package]]` record of a release crate is screened — the
/// leg collects every package block as a name→source multimap, never
/// a last-wins map.
///
/// The reported defect
/// (`lockfile-leg-validates-only-last-package-block-per-name`): the
/// leg built `dict(re.findall(...))` over name/source pairs, so every
/// same-name record collapsed to the last match. A `dcs-model`
/// recorded a second time at a divergent `rev` pin — a recording a
/// transitive dependency can legitimately introduce — was refused
/// only when it happened to sort last: divergent-first passed,
/// divergent-last refused, identical content yielding opposite
/// verdicts purely by block order. Cargo's canonical package order
/// sorts `?rev=` before `?tag=`, so the hiding order is the realistic
/// one. The doctored lockfiles plant the divergent record ahead of
/// and behind the pinned one — both must refuse, naming the divergent
/// sources — plus the sourceless twin (issue 1450's path-package
/// record, which never reached the regex at all) in both positions,
/// each on the leak exit status.
#[test]
fn every_record_of_a_release_crate_is_screened() {
    let copy = Materialized::new();
    let lock = copy.dir.join("Cargo.lock");
    let committed = std::fs::read_to_string(&lock).unwrap();

    // The leg's own verdict, run directly against each doctored copy.
    let leg = |doctored: &str| -> Output {
        std::fs::write(&lock, doctored).unwrap();
        Command::new("python3")
            .arg("ci/lockfile.py")
            .arg("Cargo.lock")
            .arg(&copy.remote)
            .arg("")
            .current_dir(&copy.dir)
            .output()
            .expect("python3 runs the lockfile leg")
    };

    // dcs-model's committed package block — the record a second,
    // divergent recording duplicates.
    let start = committed
        .find("[[package]]\nname = \"dcs-model\"\n")
        .expect("the committed lockfile records a dcs-model package block");
    let end = committed[start..]
        .find("\n[[package]]")
        .map(|i| start + i + 1)
        .unwrap_or(committed.len());
    let block = &committed[start..end];
    let source_line = block
        .lines()
        .find(|line| line.starts_with("source = \""))
        .expect("dcs-model's package block records a source");
    // The divergent recording: the same crate at another rev — the
    // `upgrade` stage's recorded baseline — on the manifest's own
    // remote, so only the pin disagrees.
    let baseline = recorded_upgrade_from(&copy.dir);
    let divergent = block.replacen(
        source_line,
        &format!("source = \"git+{}?rev={baseline}#{baseline}\"", copy.remote),
        1,
    );
    assert_ne!(
        divergent, block,
        "the divergent record is indistinguishable from the committed block"
    );
    for (case, doctored) in [
        (
            "the divergent record first",
            format!(
                "{}{}{}",
                &committed[..start],
                divergent,
                &committed[start..]
            ),
        ),
        (
            "the divergent record last",
            format!("{}{}{}", &committed[..end], divergent, &committed[end..]),
        ),
    ] {
        let output = leg(&doctored);
        assert!(
            !output.status.success(),
            "{case}: a lockfile recording a release crate at a second, divergent pin \
             passed the leg"
        );
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            stderr.contains("different sources"),
            "{case}: the duplicated record was refused without naming the divergent \
             sources:\n{stderr}"
        );
    }

    // The sourceless twin: a path package records no `source` key at
    // all — the record that never reached the collected map under the
    // regex. Either position is the same leak finding.
    let sourceless = block.replacen(&format!("{source_line}\n"), "", 1);
    assert_ne!(
        sourceless, block,
        "the sourceless twin is indistinguishable from the committed block"
    );
    for (case, doctored) in [
        (
            "the sourceless record first",
            format!(
                "{}{}{}",
                &committed[..start],
                sourceless,
                &committed[start..]
            ),
        ),
        (
            "the sourceless record last",
            format!("{}{}{}", &committed[..end], sourceless, &committed[end..]),
        ),
    ] {
        let output = leg(&doctored);
        assert_eq!(
            output.status.code(),
            Some(2),
            "{case}: a sourceless duplicate of a release crate is not the leak \
             finding's exit status:\n{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
}

/// The lockfile leg reads the manifest through `cargo metadata
/// --no-deps` and the lockfile as parsed TOML — never a positional
/// spelling — so a TOML-equivalent respelling of the shipped
/// declaration is not a `lockfile-stale` or `path-dependency-leak`
/// finding. This is the reported
/// `lockfile-leg-positional-toml-regex-misdiagnoses` reproduction:
/// the `tag` fragment spelled before `git` in the inline table, the
/// pin broken across the table's lines, an inert commented-out pin
/// beside the real declaration, and `dcs-core`'s `source` field
/// recorded after its `dependencies` list are each the same pinned
/// declaration — the leg must pass every one. The boundary still
/// stands the other way: a release crate recorded with no `source`
/// at all resolves from a path into some checkout, and the check
/// names it `path-dependency-leak`.
#[test]
fn toml_equivalent_respellings_pass_the_lockfile_leg() {
    let copy = Materialized::new();
    let manifest = copy.dir.join("Cargo.toml");
    let lock = copy.dir.join("Cargo.lock");
    let committed_manifest = std::fs::read_to_string(&manifest).unwrap();
    let committed_lock = std::fs::read_to_string(&lock).unwrap();

    // The materialized manifest's dependency line for a release crate,
    // broken out as its `git` remote and `tag`/`rev` fragment.
    let dep_line = |name: &str| {
        let line = committed_manifest
            .lines()
            .find(|line| line.starts_with(&format!("{name} = {{")))
            .unwrap_or_else(|| panic!("the materialized manifest declares no {name} inline table"))
            .to_owned();
        let quoted = |key: &str| {
            let marker = format!("{key} = \"");
            let start = line
                .find(&marker)
                .unwrap_or_else(|| panic!("{name}'s dependency declares no '{marker}': {line}"))
                + marker.len();
            line[start..start + line[start..].find('"').unwrap()].to_owned()
        };
        let kind = ["tag", "rev"]
            .into_iter()
            .find(|kind| line.contains(&format!("{kind} = \"")))
            .unwrap_or_else(|| panic!("{name}'s dependency declares no pin: {line}"));
        let spec = (quoted("git"), kind.to_owned(), quoted(kind));
        (line, spec.0, spec.1, spec.2)
    };
    // The leg's own verdict: exit status 0 records the declared pin.
    let leg = |case: &str| {
        let output = Command::new("python3")
            .arg("ci/lockfile.py")
            .arg("Cargo.lock")
            .arg(&copy.remote)
            .arg("")
            .current_dir(&copy.dir)
            .output()
            .expect("python3 runs the lockfile leg");
        assert!(
            output.status.success(),
            "{case}: a TOML-equivalent respelling was refused:\n{}",
            String::from_utf8_lossy(&output.stderr)
        );
    };

    // The inline table's `tag`/`rev` fragment spelled before `git` —
    // the same declaration in another key order.
    let mut respelled = committed_manifest.clone();
    for name in ["dcs-build", "dcs-model"] {
        let (line, git, kind, value) = dep_line(name);
        respelled = respelled.replacen(
            &line,
            &format!("{name} = {{ {kind} = \"{value}\", git = \"{git}\" }}"),
            1,
        );
    }
    std::fs::write(&manifest, &respelled).unwrap();
    leg("the pin fragments spelled `tag`/`rev` before `git`");

    // The same pin broken across the inline table's lines.
    let mut respelled = committed_manifest.clone();
    for name in ["dcs-build", "dcs-model"] {
        let (line, git, kind, value) = dep_line(name);
        respelled = respelled.replacen(
            &line,
            &format!("{name} = {{\n    git = \"{git}\",\n    {kind} = \"{value}\"\n}}"),
            1,
        );
    }
    std::fs::write(&manifest, &respelled).unwrap();
    leg("the pin broken across the inline table's lines");

    // An inert commented-out pin at an old rev beside the real
    // declaration — a comment declares nothing.
    let (line, git, _, _) = dep_line("dcs-build");
    std::fs::write(
        &manifest,
        committed_manifest.replacen(
            &line,
            &format!(
                "# dcs-build = {{ git = \"{git}\", rev = \"{}\" }}\n{line}",
                "0".repeat(40)
            ),
            1,
        ),
    )
    .unwrap();
    leg("an inert commented-out pin beside the real declaration");
    std::fs::write(&manifest, &committed_manifest).unwrap();

    // dcs-core's `source` field recorded after its `dependencies`
    // list — the same `[[package]]` record in another field order.
    let start = committed_lock
        .find("[[package]]\nname = \"dcs-core\"\n")
        .expect("the committed lockfile records a dcs-core package block");
    let end = committed_lock[start..]
        .find("\n[[package]]")
        .map(|i| start + i)
        .unwrap_or(committed_lock.len());
    let block = &committed_lock[start..end];
    let source_line = block
        .lines()
        .find(|line| line.starts_with("source = \""))
        .expect("dcs-core's package block records a source");
    let mut fields: Vec<&str> = block.lines().filter(|line| *line != source_line).collect();
    let close = fields
        .iter()
        .position(|line| *line == "]")
        .expect("dcs-core's package block carries a dependencies list")
        + 1;
    fields.insert(close, source_line);
    std::fs::write(
        &lock,
        format!(
            "{}{}{}",
            &committed_lock[..start],
            fields.join("\n"),
            &committed_lock[end..]
        ),
    )
    .unwrap();
    leg("the lockfile's `source` field recorded after `dependencies`");

    // The diagnostic boundary still stands: a release crate recorded
    // with no `source` at all resolves from a path into some
    // checkout — `path-dependency-leak`'s finding, which the check
    // names.
    let fields: Vec<&str> = block
        .lines()
        .filter(|line| !line.starts_with("source = \""))
        .collect();
    std::fs::write(
        &lock,
        format!(
            "{}{}{}",
            &committed_lock[..start],
            fields.join("\n"),
            &committed_lock[end..]
        ),
    )
    .unwrap();
    let output = Command::new("python3")
        .arg("ci/lockfile.py")
        .arg("Cargo.lock")
        .arg(&copy.remote)
        .arg("")
        .current_dir(&copy.dir)
        .output()
        .expect("python3 runs the lockfile leg");
    assert_eq!(
        output.status.code(),
        Some(2),
        "a release crate recorded with no source is not the leak finding's exit status:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let mut refused_cmd = Command::new("bash");
    refused_cmd
        .arg("ci/check.sh")
        .current_dir(&copy.dir)
        .env("DCS_REMOTE", &copy.remote)
        .env("DCS_RECORD_DIR", root().join("docs/releases"))
        .env("CARGO_TARGET_DIR", copy.dir.join("target"));
    let refused = run_check(&mut refused_cmd, &copy.remote);
    let stderr = String::from_utf8_lossy(&refused.stderr);
    assert!(
        !refused.status.success(),
        "a lockfile recording a release crate without a source passed the check"
    );
    assert!(
        stderr.contains("path-dependency-leak"),
        "a source-less release crate record was refused without its named diagnostic:\n{stderr}"
    );
    assert!(
        !String::from_utf8_lossy(&refused.stdout).contains("== resolve =="),
        "the leaked path source was caught only after the resolve stage:\n{stderr}"
    );
}

/// The release contract's `dcs-core`, `dcs-model` direct declarations
/// are optional — `docs/release-contract.md`: "a consumer may also
/// declare them directly — e.g. to assert
/// `dcs_model::MODEL_VERSION` — under the same pin". A manifest
/// naming neither resolves both through `dcs-build` at that one pin,
/// and the committed lockfile's record of all three release crates is
/// what the lockfile leg holds to it.
///
/// The reported defect (`lockfile-leg-misdiagnoses-omitted-direct-
/// dep`): the leg's manifest census required both `dcs-build` *and*
/// `dcs-model` as direct dependencies, turning this template's own
/// declaration shape into a correctness gate — a consumer that dropped
/// the direct `dcs-model` line was reported `lockfile-stale` (exit 1,
/// "dcs-model declares no `git = ..., tag|rev = ...` pin in
/// Cargo.toml") over a lockfile recording all three crates correctly
/// at the declared pin. The reproduction is the reported one: the
/// direct `dcs-model` dependency line deleted from the materialized
/// manifest, the committed lockfile untouched. The leg must record the
/// pin — on that committed artifact and on the lockfile `cargo update`
/// writes for the doctored manifest alike.
#[test]
fn an_omitted_optional_direct_release_dep_passes_the_lockfile_leg() {
    let copy = Materialized::new();
    let manifest = copy.dir.join("Cargo.toml");
    let lock = copy.dir.join("Cargo.lock");
    let committed_manifest = std::fs::read_to_string(&manifest).unwrap();
    let committed_lock = std::fs::read_to_string(&lock).unwrap();

    let declared = committed_manifest
        .lines()
        .find(|line| line.starts_with("dcs-model = {"))
        .unwrap_or_else(|| {
            panic!("the materialized manifest declares no direct dcs-model dependency")
        })
        .to_owned();
    let omitted = committed_manifest.replace(&format!("{declared}\n"), "");
    assert_ne!(
        omitted, committed_manifest,
        "the doctor left the direct dcs-model dependency in place"
    );
    assert!(
        !omitted.contains("dcs-model = {"),
        "the doctored manifest still declares dcs-model directly"
    );
    // The lockfile the omitted declaration must still satisfy: all
    // three release crates recorded, each from the manifest's own pin —
    // the condition the leg holds to, and the one `cargo fetch
    // --locked` later re-checks.
    let pin = pinned_release(&copy.dir);
    let precise = committed_lock_rev(&copy.dir);
    for name in ["dcs-build", "dcs-core", "dcs-model"] {
        assert!(
            committed_lock.contains(&format!("[[package]]\nname = \"{name}\"\n")),
            "the committed lockfile records no {name} package block"
        );
    }
    assert_eq!(
        committed_lock
            .matches(&format!("?tag={pin}#{precise}"))
            .count()
            + committed_lock
                .matches(&format!("?rev={pin}#{precise}"))
                .count(),
        3,
        "the committed lockfile does not record all three release crates at {pin}#{precise}"
    );
    std::fs::write(&manifest, &omitted).unwrap();

    // The leg's own verdict: exit status 0 records the declared pin.
    let passes_the_leg = |case: &str| {
        let output = Command::new("python3")
            .arg("ci/lockfile.py")
            .arg("Cargo.lock")
            .arg(&copy.remote)
            .arg("")
            .current_dir(&copy.dir)
            .output()
            .expect("python3 runs the lockfile leg");
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            output.status.success(),
            "{case}: a manifest omitting the contract-optional direct dcs-model \
             declaration was refused on a lockfile recording all three release \
             crates at the declared pin:\n{stderr}"
        );
        assert!(
            !stderr.contains("declares no"),
            "{case}: the omitted optional declaration was diagnosed as a missing pin:\n{stderr}"
        );
        let stdout = String::from_utf8_lossy(&output.stdout);
        assert!(
            stdout.contains(&precise),
            "{case}: the leg recorded no revision the lockfile carries: {stdout}"
        );
    };
    // The reported reproduction, verbatim: the committed lockfile
    // untouched.
    passes_the_leg("the committed lockfile, untouched");

    // The same lockfile still satisfies that manifest once the file is
    // the one `cargo update` writes for it: Cargo records the consumer
    // package's own dependency list, so a consumer that drops the line
    // regenerates the file — that one entry drops, every `[[package]]`
    // record stays — and no re-resolve is needed afterwards, because the
    // omitted declaration removes no resolved package. What the leg
    // refused was therefore a shape, not a disagreement.
    let root_start = committed_lock
        .find("[[package]]\nname = \"pump-station\"\n")
        .expect("the committed lockfile records the consumer package");
    let root_end = committed_lock[root_start..]
        .find("\n[[package]]")
        .map(|at| root_start + at)
        .unwrap_or(committed_lock.len());
    let regenerated = format!(
        "{}{}{}",
        &committed_lock[..root_start],
        committed_lock[root_start..root_end].replacen(" \"dcs-model\",\n", "", 1),
        &committed_lock[root_end..],
    );
    assert_ne!(
        regenerated, committed_lock,
        "the doctor left the consumer package's own dcs-model dependency entry in place"
    );
    std::fs::write(&lock, &regenerated).unwrap();
    passes_the_leg("the regenerated lockfile");
    let before = std::fs::read(&lock).unwrap();
    let metadata = Command::new(CARGO)
        .args(["metadata", "--locked", "--format-version", "1"])
        .current_dir(&copy.dir)
        .env("CARGO_TARGET_DIR", copy.dir.join("target"))
        .output()
        .expect("cargo metadata runs");
    assert!(
        metadata.status.success(),
        "the regenerated Cargo.lock does not satisfy a manifest without the direct \
         dcs-model declaration: {}",
        String::from_utf8_lossy(&metadata.stderr)
    );
    assert_eq!(
        std::fs::read(&lock).unwrap(),
        before,
        "the locked resolve rewrote the regenerated Cargo.lock"
    );
    let graph = String::from_utf8_lossy(&metadata.stdout);
    for name in ["dcs-build", "dcs-core", "dcs-model"] {
        assert!(
            graph.contains(&format!("\"name\":\"{name}\"")),
            "{name} is absent from the resolved graph of a manifest declaring only \
             dcs-build directly"
        );
    }
    assert!(
        graph.contains(&format!("git+{}?", copy.remote)),
        "the resolved graph carries no release crate from the pinned remote"
    );
    std::fs::write(&manifest, &committed_manifest).unwrap();
}

/// The `upgrade` stage is the executable assertion of the documented
/// repin upgrade (README §7): under the same `file://`-remote and
/// binary substitutions as the other stages, the stage materializes
/// the unchanged tree at the previous release's recorded rev, repins
/// it to the recorded release — the stand-in's tag resolving to the
/// revision the committed lockfile records for it — proves the emitted
/// `model/plant.json` is
/// byte-identical across the repin (`emit-divergent` stands guard),
/// re-runs the full pipeline under the repin, and refuses the named
/// incompatible crossings — the nonexistent tag (`pin-unresolvable`)
/// and the pin outside the supported `MODEL_VERSION`/`version` window
/// (`crossing-unrefused`).
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
        .arg("ci/legs/pair.py")
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
        .arg("ci/legs/negotiation.py")
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
        .arg("ci/legs/refusal.py")
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
            .arg("ci/legs/handover.py")
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
        .arg("ci/legs/takeover.py")
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
        .arg("ci/legs/divergence.py")
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
    let schema = root().join("docs/releases/v0.3.0/block-interfaces.schema.json");
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
/// against is the `scenario-failed` diagnostic. The doctored copy
/// re-records the manifest's `dynamics.fingerprint` over the changed
/// bytes — a customer who revises the dynamics re-approves the
/// document — so the fingerprint stage holds and the run reaches the
/// simulate stage.
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
    let fingerprint = Command::new("python3")
        .arg("ci/dynamics_fingerprint.py")
        .arg("--fingerprint")
        .arg("model/dynamics.json")
        .current_dir(&copy.dir)
        .output()
        .expect("python3 fingerprints the doctored dynamics");
    assert!(
        fingerprint.status.success(),
        "the fingerprint helper failed: {}",
        String::from_utf8_lossy(&fingerprint.stderr)
    );
    let fingerprint = String::from_utf8_lossy(&fingerprint.stdout);
    let fingerprint = fingerprint.trim();
    let manifest = copy.dir.join("deploy/manifest.json");
    let source = std::fs::read_to_string(&manifest).unwrap();
    let parsed: serde_json::Value = serde_json::from_str(&source).unwrap();
    let recorded = parsed["dynamics"]["fingerprint"].as_str().unwrap();
    let field = format!("\"fingerprint\": \"{recorded}\"");
    assert!(
        source.contains(&field),
        "the manifest's dynamics fingerprint field moved"
    );
    std::fs::write(
        &manifest,
        source.replacen(&field, &format!("\"fingerprint\": \"{fingerprint}\""), 1),
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

/// A checked-in dynamics document diverging from the manifest's
/// recorded `dynamics.fingerprint` is the `manifest-fingerprint-mismatch`
/// diagnostic — the deployment declaration no longer names the approved
/// dynamics bytes.
#[test]
fn a_doctored_dynamics_reports_manifest_fingerprint_mismatch() {
    let tools = build_tools();
    let copy = Materialized::new();
    let dynamics = copy.dir.join("model/dynamics.json");
    let source = std::fs::read_to_string(&dynamics).unwrap();
    std::fs::write(
        &dynamics,
        source.replacen("\"off_rate\": 0.25", "\"off_rate\": 0.0", 1),
    )
    .unwrap();
    let output = copy.check(&tools);
    assert!(
        !output.status.success(),
        "a doctored dynamics document passed"
    );
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("manifest-fingerprint-mismatch"),
        "expected the manifest-fingerprint-mismatch diagnostic, got:\n{}",
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

# A journal-retained kind-emitted event absent from the journal is
# named.
event = {
    "name": "sequence_completed",
    "payload": [{"name": "steps", "kind": "int"}],
    "retention": "journal",
    "adapted": "declared",
}
failures = simulate.emitted_event_misses([("sequencer:39", event)], [])
assert any("sequence_completed" in f for f in failures), failures

# A history/latest-retained event landing in the journal is named —
# the durable record carries no routed emission.
routed = {
    "name": "step_completed",
    "payload": [{"name": "step", "kind": "int"}],
    "retention": "history",
    "adapted": "declared",
}
failures = simulate.emitted_event_misses(
    [("sequencer:39", routed)],
    [
        {
            "seq": 1,
            "tick": 1,
            "event": {
                "event_emitted": {
                    "event": {
                        "event": "step_completed",
                        "component": "sequencer:39",
                        "fields": {"step": {"value": {"int": 1}}},
                    }
                }
            },
        }
    ],
)
assert any("step_completed" in f for f in failures), failures

# A kind-emitted event missing from the instance's resource view is
# named.
failures = simulate.resource_event_misses(
    [("sequencer:39", routed)],
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
/// every `<leg>-unchecked` self-check diagnostic the check emits —
/// `ci/check.sh`'s inline `fail` names and the pair stage's legs,
/// whose driver emits `<stem>-unchecked` for each `ci/legs/*.py`
/// file's declared doctored cases, the stem the file's name with its
/// underscores turned to dashes. An operator or tool reading a
/// self-check failure resolves the name against the declared
/// contract, so an emitted name the vocabulary does not declare is an
/// unnamed diagnostic by another name. An emitted `<stem>-unchecked`
/// is covered when the contract declares the name itself, or when the
/// declared `<leg>-unchecked` convention covers it — the convention
/// entry present and the leg's own `<stem>` or `<stem>-failed`
/// diagnostic declared.
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
    // The pair stage's legs emit `<stem>-unchecked` through
    // `ci/legs.py`'s driver rather than the script's `fail` lines —
    // every leg file's stem is an emitted name.
    for entry in std::fs::read_dir(root().join("reference-plant/ci/legs")).unwrap() {
        let entry = entry.unwrap();
        let name = entry.file_name().into_string().unwrap();
        if !name.ends_with(".py") {
            continue;
        }
        let stem = name[..name.len() - 3].replace('_', "-");
        let emitted_name = format!("{stem}-unchecked");
        if !emitted.contains(&emitted_name) {
            emitted.push(emitted_name);
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
