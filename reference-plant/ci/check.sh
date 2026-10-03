#!/usr/bin/env bash
# The reference plant's clean-CI check: everything a fresh clone of this
# repository runs with no platform checkout present.
#
# Stages, each reporting the release contract's named diagnostics on
# failure (docs/release-contract.md):
#
#   lockfile     the committed Cargo.lock satisfies the manifest this
#                repository ships — every `dcs-*` record of a release
#                crate carried by a git source, never a path package
#                into a checkout, which records no source at all
#                (path-dependency-leak), on the pin's own remote and
#                the same `tag`/`rev` fragment Cargo.toml spells, one
#                precise revision across all three — at the revision
#                the declared pin names: the tag's target read back
#                off the remote, the declared full-sha rev, or the
#                release record's filled Commit field. Read before
#                any fetch can rewrite the file, so a committed
#                lockfile that no longer records the pin is reported
#                rather than silently re-resolved
#                (lockfile-stale); the leg's own doctored copies —
#                a lockfile recorded at another revision and one
#                missing a release crate's package block — must each
#                report that diagnostic (lockfile-stale-unchecked).
#                The stage's digest of the file is what the resolve
#                stage re-checks, naming a rewrite the fallback fetch
#                performs on the committed artifact.
#   resolve      cargo fetch --locked — the pinned release crates
#                resolve against the committed lockfile, so every
#                build resolves the same sources
#                (pin-unresolvable; lockfile-stale when the documented
#                re-resolve fallback had to rewrite the artifact)
#   build        cargo build — the composition compiles against the
#                supported surface (surface-incompatible)
#   emit         the model and scenario emit byte-identically twice and
#                match the checked-in artifacts (emit-nondeterministic,
#                stale-artifact)
#   tooling      the released tooling accepts the emitted model —
#                `dcs-model validate`, `dcs-model lint`,
#                `dcs-controller --check` — and the dynamics document
#                standalone, `dcs-plant-server --check-dynamics` merging
#                and validating every element against the model's
#                channel map with no server launched — and exercises the
#                contract's
#                remaining dcs-model surfaces: `dcs-model schema`,
#                `dcs-model interface-schema`, `dcs-model deploy-schema`,
#                and `dcs-plant-server --dynamics-schema` emissions
#                byte-identical to the release record's schema artifacts
#                (fetched from the pinned revision through the same git
#                remote the pins resolve over), the checked-in
#                deploy/manifest.json and model/dynamics.json screened
#                against their declared schemas with doctored
#                schema-violating copies refused, `dcs-model diff`
#                naming a doctored
#                compatible revision's changes and none on the
#                identical document, and `dcs-model summary` /
#                `dcs-model signal-index` outputs recorded to the run's
#                evidence (tooling-rejected, pin-unresolvable,
#                schema-drift, schema-mismatch,
#                schema-mismatch-nondeterministic, diff-mismatch)
#   alarm-validation
#                the rejection half of decision 70's alarm record at
#                the customer boundary — every managed alarm instance
#                carrying its rationalization prose and
#                priority/class/response_ticks codes, doctored copies
#                refused by the released `dcs-controller --check`
#                naming the missing element, and the driven run's
#                served components/parameters sections reporting the
#                same record (alarm-validation-failed,
#                alarm-validation-nondeterministic)
#   fingerprint  the emitted model's fingerprint equals the manifest's
#                recorded `model.fingerprint`, the deployed pair's
#                served model digest — each peer's /checkpoint-stamped
#                fingerprint on the manifest-declared deployment —
#                equals it too, so the record authorizes the served
#                model bytes rather than only the checked-in file; a
#                doctored served document — every point id renumbered
#                over identical components — reports the named
#                mismatch carrying the expected vs served fingerprint
#                and the first diverging section; and the dynamics
#                document the manifest-declared deployment serves —
#                the `dynamics.path` the rig mounts and
#                `dcs-plant-server --dynamics` merges — fingerprints
#                the recorded optional `dynamics.fingerprint` and the
#                checked-in artifact identically on the launched
#                pair, a doctored served document with renumbered
#                point references over identical element content
#                reporting the named mismatch; two passes produce
#                identical digests (manifest-fingerprint-mismatch,
#                fingerprint-failed, fingerprint-nondeterministic,
#                fingerprint-unchecked, dynamics-fingerprint-failed,
#                dynamics-fingerprint-nondeterministic,
#                dynamics-fingerprint-unchecked)
#   deploy       the checked-in rig definition deploy/compose.yaml
#                instantiates every field of deploy/manifest.json —
#                release, images, mounted model and dynamics paths,
#                the fingerprint propagated into the controller
#                invocations, listen addresses, the pair's standby
#                wiring, the standby's optional failover_budget
#                declaration carried as its --auto-promote flag, and
#                the optional per-controller persistence
#                paths (state_file/journal_file/history_file) backed
#                by writable
#                mounts and flags, the declared topology
#                section's pairs — the checked-in manifest naming
#                the deployed pair under topology.pairs, each pair
#                naming two declared members whose standby wiring
#                closes inside it — and the one-field-per-deployment
#                bound (decision 99): at most one field-owning duty
#                controller over the manifest's single plant, a
#                second claimant — a second declared pair's duty
#                member included — being undeployable; parsed and
#                validated through `docker compose config` or the
#                fallback parser, with the fields' divergence cases
#                exercised against doctored copies
#                (rig-invalid, rig-unverifiable, rig-mismatch)
#   simulate     the scripted simulation's declared outcomes hold, and
#                two runs produce identical digests (scenario-failed,
#                scenario-nondeterministic)
#   restart      the restart-recovery leg (WW-LCM-001's
#                lone-controller clause): the field-owning controller
#                runs the deterministic scenario on the
#                manifest-declared
#                --state-file/--journal-file/--history-file flags
#                pointed at runner-owned scratch paths, is stopped at
#                a leg boundary, and relaunches onto the same files —
#                the resumed run must continue at the persisted tick
#                with leg outcomes, receipts, and the field image
#                equal to an uninterrupted reference pass, the
#                journal's seq order continuing across the file's
#                run-boundary marker; a missing or unparseable state
#                file never passes silently
#                (restart-resume-failed, restart-resume-nondeterministic)
#   surface      the served operator surface — the signal index, the
#                monitoring page, the snapshot's descriptors, the
#                block-interface registry covering every declared
#                component, the kind-declared commands answering
#                structured receipts through POST /command and
#                reporting their published availability verdicts
#                through GET /resources in both directions, and the
#                kind-emitted events reaching the journal and the
#                per-instance resource view — matches the emitted
#                model's declaration, in the same deterministic
#                --driven run the simulate stage performs — plus the
#                served GET /schema document's structural conformance
#                to the fetched block-interfaces schema artifact
#                (surface-mismatch, schema-mismatch)
#   pair         the consumer-declared redundant pair — the deployment
#                the manifest actually declares, not just its
#                definition, run on the released tooling. The stage's
#                legs are the ci/legs/*.py files — one file per leg,
#                each carrying its explanatory prose in its own
#                docstring and its stage registration in a module-level
#                LEG literal (its declared order, its titles, any extra
#                tool flags, and its doctored cases). ci/legs.py
#                discovers the legs in declared order, runs each twice
#                requiring identical digests, then runs each declared
#                tamper requiring its named evidence; the legs share
#                the launch/settle/restore harness consolidated under
#                #647 — ci/legs/pair.py's launch_pair/PairRig — and
#                each restores the pair's launch roles for the next.
#                Every leg reports its own <stem>-failed /
#                <stem>-nondeterministic / <stem>-unchecked
#                diagnostics, the stem its file name with underscores
#                turned to dashes; adding a leg is one new file under
#                ci/legs/ — this script, the boundary lint, and the
#                README need no edit. A leg declaring `upgrade_tools`
#                additionally runs its flag arguments against the
#                recorded upgrade-from revision's tooling, resolved
#                through the same cargo-install mechanism at
#                $DCS_UPGRADE_REV — the rolling-upgrade leg's
#                predecessor-release binaries.
#   consumers    the replaceable-consumer boundary: the simulate
#                stage's deterministic driven run replays under each
#                consumer schedule — no UI attached, normal polling, a
#                stalled reader, disconnect/reconnect churn, malformed
#                and flooded traffic within the declared limits, and a
#                UI process restart — producing identical output and
#                receipt digests across every schedule and across two
#                passes (consumer-interference, consumer-nondeterministic)
#   ctl          the shipped operator CLI against the same driven run:
#                dcs-ctl's reads answer the served contract — signals,
#                schema, snapshot, events, resources — its invoke
#                submits the sequencer's kind-declared advance/reset
#                through the bounded receipted path with actor
#                attribution, settling applied receipts visible through
#                receipts and the journal, and the refusal modes —
#                undeclared command, malformed argument, unreachable
#                monitor — exit nonzero naming the failure; two passes
#                produce identical digests (ctl-failed,
#                ctl-nondeterministic)
#   upgrade      the documented repin upgrade (README §7): this tree's
#                composition is materialized pinned at the previous
#                release's recorded rev, repinned to the recorded
#                release rev — the crossing the manifest's dcs_release
#                names — and re-emitted; the bytes must equal the
#                checked-in model/plant.json, and the full pipeline
#                re-runs under the repin; the named incompatible
#                crossings are refused
#                (emit-divergent, pin-unresolvable, crossing-unrefused)
#
# Environment:
#
#   DCS_REMOTE   the git remote the release crates and tooling resolve
#                from (default: the published origin below). The
#                workspace-side proof substitutes a file:// stand-in and
#                rewrites this repository's Cargo.toml — and its
#                committed Cargo.lock, whose recorded remote must keep
#                matching the manifest's — to match.
#   DCS_REV      the pinned revision (default: the release tag this
#                repository's manifest records — v0.10.0, resolving to
#                the recorded commit whose tooling serves the interface
#                registry, declared commands and their live availability
#                verdicts, and routed emitted events the surface stage
#                proves, beside the pair and receipt contracts the legs
#                exercise, the corrected claim/tracking/failover
#                arbitration and bounded-liveness contracts, the
#                tracking-source rediscovery, driven-scan bound,
#                status-line label, history backfill, born-active
#                startup-failure, and durable process-history contracts,
#                the demote released-claim hand-back, persistence-path
#                distinctness, deferred startup-claim refusal, and
#                remote correspondence-gate contracts, and the bounded
#                contact backoff, convergence-gated reclaim,
#                self-address refusal, sim-bus claim family, named
#                standby remedy, announced-source verification, skew
#                bound, and checkpoint-pull recovery contracts the
#                mirror legs gate on, beside the checkpoint path's
#                cross-peer single-writer refusal and the promotion
#                claim's basis skew bound this pin's mirror legs
#                gate on).
#   DCS_UPGRADE_REV
#                the earlier compatible revision the upgrade stage
#                materializes the tree at before repinning to $DCS_REV
#                (default: the earliest release-line rev whose builder
#                API carries the composition's declared dimensional
#                metadata — `PlantBuilder::unit`/`port_unit`/
#                `param_unit` — so the stage proves the recorded
#                rev → tag crossing the manifest names from a
#                baseline this tree's own source still compiles
#                against; the workspace-side proof seeds its stand-in
#                remote to serve it).
#   DCS_UPGRADE  set to 0 to skip the upgrade stage — the stage's own
#                repinned re-run uses this internally.
#   DCS_TOOLS    a directory holding prebuilt `dcs-model`,
#                `dcs-controller`, `dcs-plant-server`, `dcs-plant-ctl`,
#                `dcs-ctl`, and `dcs-alarm-report` binaries. When
#                unset, the check installs them from $DCS_REMOTE at
#                $DCS_REV — the contract's `cargo install --git`
#                mechanism — into a scratch root.
#   DCS_RECORD_DIR
#                a directory holding the release record tree
#                (`docs/releases/<tag>/…`) the schema-drift leg
#                compares the tooling's emissions against. When unset
#                — the contract's own shape — the record is fetched
#                from $DCS_REMOTE at $DCS_REV. The workspace-side
#                proof substitutes the checkout's own docs/releases:
#                its tooling stand-ins emit the checkout's schemas,
#                which legitimately drift from the pinned release's
#                recorded artifacts between cuts (the contract's
#                additive serde-optional fields land without a version
#                bump), so the record the checkout carries is the
#                comparator. The pinned-rev fetch still runs either
#                way, proving the record stays reachable through the
#                consumer mechanism.

set -euo pipefail
cd "$(dirname "$0")/.."

DCS_REMOTE="${DCS_REMOTE:-https://github.com/Jan-Kaspar1/dcs.git}"
DCS_REV="${DCS_REV:-v0.10.0}"
DCS_UPGRADE_REV="${DCS_UPGRADE_REV:-07ec24f94dfaf5ff42d56a614718190da3403fd5}"
DCS_TOOLS="${DCS_TOOLS:-}"
DCS_RECORD_DIR="${DCS_RECORD_DIR:-}"
TOOLS=""
TOOLS_REV=""
UPGRADE_DIR=""
INSTALL_ROOTS=""
RIG_DIR=""
SCRATCH=""

fail() {
    echo "$1" >&2
    exit 1
}

cleanup() {
    if [ -n "$UPGRADE_DIR" ]; then rm -rf "$UPGRADE_DIR"; fi
    if [ -n "$RIG_DIR" ]; then rm -rf "$RIG_DIR"; fi
    if [ -n "$SCRATCH" ]; then rm -rf "$SCRATCH"; fi
    for dir in $INSTALL_ROOTS; do rm -rf "$dir"; done
}
trap cleanup EXIT

# Resolves the released tooling's binaries into $TOOLS: the caller's
# $DCS_TOOLS directory when set, else `cargo install --git $DCS_REMOTE
# --rev <$1>` into a tracked scratch root — the contract's install
# mechanism — cached by the revision already resolved.
ensure_tools() {
    if [ -n "$DCS_TOOLS" ]; then
        TOOLS="$DCS_TOOLS"
        return
    fi
    if [ "$TOOLS_REV" = "$1" ] && [ -n "$TOOLS" ]; then return; fi
    local dir
    dir="$(mktemp -d)"
    if ! cargo install --quiet --git "$DCS_REMOTE" --rev "$1" \
            dcs-model dcs-controller dcs-plant dcs-monitor dcs-sim-net \
            --root "$dir"; then
        rm -rf "$dir"
        return 1
    fi
    # The delivered binary set — the `dcs-monitor` package ships both
    # operator tools, `dcs-ctl` and `dcs-alarm-report`, and
    # `dcs-sim-net` ships the plant-side `dcs-plant-ctl`; a revision
    # whose tooling predates one fails the install rather than the
    # stage that invokes it.
    local bin
    for bin in dcs-model dcs-controller dcs-plant-server dcs-ctl \
            dcs-alarm-report dcs-plant-ctl; do
        if [ ! -x "$dir/bin/$bin" ]; then
            rm -rf "$dir"
            return 1
        fi
    done
    INSTALL_ROOTS="$INSTALL_ROOTS $dir"
    TOOLS="$dir/bin"
    TOOLS_REV="$1"
}

# Rewrites the release-crate specifiers in $UPGRADE_DIR/Cargo.toml: $1
# replaces each dependency's pin fragment — `rev = "<sha>"`,
# `tag = "<name>"`, optionally carrying a `version = "…"` requirement —
# while the remote stays exactly as this tree records it.
repin() {
    python3 - "$UPGRADE_DIR/Cargo.toml" "$DCS_REMOTE" "$1" <<'PY'
import re, sys
path, remote, spec = sys.argv[1], sys.argv[2], sys.argv[3]
toml = open(path).read()
for name in ("dcs-build", "dcs-model"):
    toml, count = re.subn(
        name + r' = \{[^}]+\}',
        lambda _: name + ' = { git = "' + remote + '", ' + spec + ' }',
        toml,
    )
    if count != 1:
        sys.exit(f"repin: expected one {name} dependency, rewrote {count}")
open(path, "w").write(toml)
PY
}

# The committed Cargo.lock is this repository's reproducibility
# artifact: README §2's promise is that a tag pin resolves once and the
# committed lockfile records the commit it landed on, so a fresh clone
# must resolve under `cargo fetch --locked` without a resolver quietly
# repairing the file first. Cargo refuses a lockfile whose recorded
# query disagrees with the manifest's pin at all, so this leg compares
# the committed lockfile against the declared pin *before* any fetch
# can rewrite it — the state a consumer's own CI could otherwise never
# see, because the resolve stage's re-resolve fallback used to absorb
# it. ci/lockfile.py is the leg, kept as a script beside the other
# stage legs so it can run against a scratch manifest/lockfile pair on
# its own: it reads the manifest through `cargo metadata --no-deps` —
# Cargo's own TOML dialect, leaving the lockfile unread — and the
# lockfile through stdlib `tomllib`, so key order, line wrapping, and
# comments are inert and a TOML-equivalent respelling of the pinned
# declaration is the same declaration rather than a stale lockfile or
# a leaked path source.
# $1 is the lockfile to read: `Cargo.lock` itself in the positive
# leg, a doctored scratch copy in the self-check below; $2 is the
# release record's `record.md` when one was substituted, else the
# empty string. Exit status 2 is a release crate recorded from a
# non-git source or from no source at all — a `path` package into some
# checkout carries neither, and is `path-dependency-leak`'s finding —
# and 1 every other disagreement, `lockfile-stale`'s.
lockfile_leg() {
    python3 ci/lockfile.py "${1:-Cargo.lock}" "$DCS_REMOTE" "${2:-}"
}

# The leg's exit status named: a release crate recorded from a path
# into some checkout — or with no source at all, as a path package is
# written — is `path-dependency-leak`; every other
# disagreement between the committed lockfile and this repository's
# declared pin is `lockfile-stale`.
lockfile_check() {
    local status=0
    lockfile_leg "${1:-Cargo.lock}" "${2:-}" || status=$?
    case "$status" in
        0) return 0 ;;
        2) fail "path-dependency-leak: a release crate is recorded from a non-git source — or from no source at all — in ${1:-Cargo.lock}" ;;
        *) fail "lockfile-stale: ${1:-Cargo.lock} does not record this repository's declared pin" ;;
    esac
}

echo "== lockfile =="
# The digest the resolve stage re-checks: a committed lockfile this
# stage proved records the declared pin must come out of the resolve
# byte-identical, so the documented re-resolve fallback cannot absorb
# a stale artifact.
LOCK_DIGEST="$(sha256sum Cargo.lock | cut -d' ' -f1)"
LOCK_RECORD=""
if [ -n "$DCS_RECORD_DIR" ]; then
    DCS_RELEASE="$(python3 -c 'import json; print(json.load(open("deploy/manifest.json"))["dcs_release"])')"
    [ -f "$DCS_RECORD_DIR/$DCS_RELEASE/record.md" ] \
        && LOCK_RECORD="$DCS_RECORD_DIR/$DCS_RELEASE/record.md"
fi
lockfile_check Cargo.lock "$LOCK_RECORD"

# The leg must report its own diagnostic — a stale lockfile is named,
# never repaired. The doctored copy is the reported defect put back:
# the release crates recorded at an earlier revision's `rev` pin while
# this tree's manifest declares its own pin. It lives under the
# scratch root, so the committed artifact stays pristine.
STALE_LOCK="$(mktemp)"
python3 - Cargo.lock "$STALE_LOCK" "$DCS_UPGRADE_REV" <<'PY'
import re, sys
lock, stale, baseline = sys.argv[1], sys.argv[2], sys.argv[3]
doctored, count = re.subn(
    r'\?[^#"]*#[0-9a-f]{40}"', lambda _: f'?rev={baseline}#{baseline}"', open(lock).read()
)
if count != 3:
    sys.exit(f"doctor: expected three git sources to doctor, rewrote {count}")
open(stale, "w").write(doctored)
PY
if out="$(lockfile_leg "$STALE_LOCK" 2>&1)"; then
    fail "lockfile-stale-unchecked: a lockfile recorded at another revision passed the lockfile leg"
fi
case "$out" in
    *"but Cargo.toml declares"*) ;;
    *) fail "lockfile-stale-unchecked: the stale lockfile was refused without naming the pin it should record: $out" ;;
esac
echo "  a lockfile recorded at another revision refused: lockfile-stale"

# The stale row's other case: a release crate with no `[[package]]`
# block at all is `lockfile-stale`, never the leak diagnostic —
# nothing is recorded, from a path source or otherwise. The doctored
# copy drops dcs-core's package block, the reported reproduction; the
# diagnostic is asserted on the check's own report, not the leg's exit
# status, so a reversion to the leak path is caught here.
MISSING_LOCK="$(mktemp)"
python3 - Cargo.lock "$MISSING_LOCK" <<'PY'
import re, sys
lock, missing = sys.argv[1], sys.argv[2]
text = open(lock).read()
# The crate's [[package]] block, found by its header boundary rather
# than one fixed field order: a merged or hand-edited lockfile may
# carry the block's keys in another order — all of them are the same
# block under TOML.
header, *blocks = re.split(r"(?m)^\[\[package\]\]\n", text)
kept = [block for block in blocks
        if not re.search(r'(?m)^name = "dcs-core"$', block)]
if len(kept) != len(blocks) - 1:
    sys.exit(f"doctor: expected one dcs-core package block, found {len(blocks) - len(kept)}")
open(missing, "w").write(header + "".join(f"[[package]]\n{block}" for block in kept))
PY
if out="$(lockfile_check "$MISSING_LOCK" 2>&1)"; then
    fail "lockfile-stale-unchecked: a lockfile missing a release crate passed the lockfile leg"
fi
case "$out" in
    *"lockfile-stale:"*) ;;
    *) fail "lockfile-stale-unchecked: a lockfile missing a release crate was refused without the lockfile-stale diagnostic: $out" ;;
esac
echo "  a lockfile missing a release crate refused: lockfile-stale"

echo "== resolve =="
# `cargo fetch --locked` is the fast path and, with a committed
# lockfile that satisfies the manifest, it is what makes every build
# resolve the same sources. README §7's `cargo update` is the
# documented remedy for a tree whose lockfile has not been regenerated
# yet, so the fallback re-resolves once — and the digest the lockfile
# stage recorded names that rewrite on the committed artifact instead
# of absorbing it.
LOCKED_ERR="$(mktemp)"
if ! cargo fetch --locked 2>"$LOCKED_ERR"; then
    cargo fetch || {
        sed 's/^/  /' "$LOCKED_ERR" >&2
        fail "pin-unresolvable: cargo fetch failed for the pinned release"
    }
    if [ "$(sha256sum Cargo.lock | cut -d' ' -f1)" != "$LOCK_DIGEST" ]; then
        fail "lockfile-stale: the committed Cargo.lock did not satisfy the declared pin — the resolve stage re-resolved it; regenerate it with \`cargo update\` (README §7)"
    fi
fi
rm -f "$LOCKED_ERR" "$STALE_LOCK" "$MISSING_LOCK"

echo "== build =="
cargo build --quiet || {
    cargo build 2>&1 | sed 's/^/  /' >&2
    fail "surface-incompatible: the composition does not compile against the pinned release"
}
TARGET_DIR="$(cargo metadata --format-version 1 --no-deps \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["target_directory"])')"
BIN="$TARGET_DIR/debug/pump-station"

echo "== emit =="
EMIT_1="$(mktemp)"; EMIT_2="$(mktemp)"; SCEN_1="$(mktemp)"; SCEN_2="$(mktemp)"
"$BIN" > "$EMIT_1"
"$BIN" > "$EMIT_2"
cmp -s "$EMIT_1" "$EMIT_2" \
    || fail "emit-nondeterministic: two emission runs produced different bytes"
cmp -s "$EMIT_1" model/plant.json \
    || fail "stale-artifact: a fresh emit differs from the checked-in model/plant.json"
"$BIN" --scenario > "$SCEN_1"
"$BIN" --scenario > "$SCEN_2"
cmp -s "$SCEN_1" "$SCEN_2" \
    || fail "emit-nondeterministic: two scenario emissions produced different bytes"
cmp -s "$SCEN_1" ci/scenario.json \
    || fail "stale-artifact: a fresh emit differs from the checked-in ci/scenario.json"
echo "  emit is byte-stable and matches the checked-in artifacts"

echo "== tooling =="
ensure_tools "$DCS_REV" \
    || fail "pin-unresolvable: cargo install --git $DCS_REMOTE --rev $DCS_REV failed"
"$TOOLS/dcs-model" validate model/plant.json \
    || fail "tooling-rejected: dcs-model validate refused the checked-in model"
LINT="$("$TOOLS/dcs-model" lint model/plant.json)" \
    || fail "tooling-rejected: dcs-model lint refused the checked-in model"
# The checked-in model's only advisories are undeclared
# `stale_after_ticks` budgets — freshness stays an opt-in per-point
# declaration; any other finding class fails the stage.
if [[ "$LINT" != *"no findings"* ]]; then
    UNEXPECTED="$(printf '%s\n' "$LINT" \
        | grep -v '^field_input_without_freshness_budget ' || true)"
    [[ -z "$UNEXPECTED" ]] \
        || fail "tooling-rejected: dcs-model lint reports findings: $LINT"
fi
"$TOOLS/dcs-controller" model/plant.json --check \
    || fail "tooling-rejected: dcs-controller --check refused the checked-in model"
"$TOOLS/dcs-plant-server" model/plant.json --check-dynamics model/dynamics.json \
    || fail "tooling-rejected: dcs-plant-server --check-dynamics refused the checked-in dynamics document"
echo "  validate, lint, --check, and --check-dynamics accept the checked-in documents"

# The release record's schema artifacts: `docs/releases/<tag>/` lives
# in the same repository the crate and tooling pins resolve from, so
# the record is fetched through the same mechanism — the pinned
# revision's git remote. The manifest's `dcs_release` names the record
# directory; the files are read out of the fetched commit's tree.
SCRATCH="$(mktemp -d)"
DCS_RELEASE="$(python3 -c 'import json; print(json.load(open("deploy/manifest.json"))["dcs_release"])')"
git -C "$SCRATCH" init -q -b main
git -C "$SCRATCH" fetch --depth 1 --quiet "$DCS_REMOTE" "$DCS_REV" \
    || fail "pin-unresolvable: the pinned rev $DCS_REV could not be fetched for the release record"
RECORD="$SCRATCH/record"
mkdir -p "$RECORD"
RECORD_ARTIFACTS="block-interfaces.schema.json plant-model.schema.json deploy-manifest.schema.json dynamics.schema.json"
for artifact in $RECORD_ARTIFACTS; do
    git -C "$SCRATCH" show "FETCH_HEAD:docs/releases/$DCS_RELEASE/$artifact" \
        > "$RECORD/$artifact" \
        || fail "pin-unresolvable: the pinned rev serves no docs/releases/$DCS_RELEASE/$artifact"
done
if [ -n "$DCS_RECORD_DIR" ]; then
    # The workspace-side proof's tooling stand-ins emit the checkout's
    # schemas, so the checkout's own record tree is the comparator;
    # the fetch above still proves the record stays fetchable at the
    # pinned rev through the consumer mechanism.
    for artifact in $RECORD_ARTIFACTS; do
        cp "$DCS_RECORD_DIR/$DCS_RELEASE/$artifact" "$RECORD/$artifact" \
            || fail "record-missing: $DCS_RECORD_DIR serves no $DCS_RELEASE/$artifact"
    done
fi

# Each recorded schema's emitting tool run at the pinned rev must
# reproduce the recorded artifact byte-for-byte — the consumer's
# non-drift leg for the served-registry, plant-model,
# deployment-manifest, and dynamics-document schemas the contract
# records as fetchable release artifacts. $1 is the tool invocation
# (binary plus its mode arguments); $2 the recorded artifact; $3 its
# name. A divergence reports schema-drift on stderr and returns 1.
schema_nondrift() {
    local emitted
    emitted="$(mktemp)"
    if ! $1 > "$emitted"; then
        rm -f "$emitted"
        echo "tooling-rejected: $1 failed at the pinned rev" >&2
        return 1
    fi
    if ! cmp -s "$emitted" "$2"; then
        rm -f "$emitted"
        echo "schema-drift: $1 at the pinned rev does not emit the recorded $DCS_RELEASE artifact $3" >&2
        return 1
    fi
    rm -f "$emitted"
}
schema_nondrift "$TOOLS/dcs-model interface-schema" "$RECORD/block-interfaces.schema.json" block-interfaces.schema.json || exit 1
schema_nondrift "$TOOLS/dcs-model schema" "$RECORD/plant-model.schema.json" plant-model.schema.json || exit 1
schema_nondrift "$TOOLS/dcs-model deploy-schema" "$RECORD/deploy-manifest.schema.json" deploy-manifest.schema.json || exit 1
schema_nondrift "$TOOLS/dcs-plant-server --dynamics-schema" "$RECORD/dynamics.schema.json" dynamics.schema.json || exit 1
echo "  schema, interface-schema, deploy-schema, and --dynamics-schema emit the $DCS_RELEASE record's artifacts byte-identically"

# A drifted artifact must report the diagnostic — the same leg against
# a doctored copy, so the recorded file stays pristine.
DOCTORED_SCHEMA="$SCRATCH/block-interfaces.doctored.json"
python3 - "$RECORD/block-interfaces.schema.json" "$DOCTORED_SCHEMA" <<'PY'
import json, sys
document = json.load(open(sys.argv[1]))
document["required"].remove("tick")
json.dump(document, open(sys.argv[2], "w"), indent=2)
PY
if out="$(schema_nondrift "$TOOLS/dcs-model interface-schema" "$DOCTORED_SCHEMA" block-interfaces.schema.json 2>&1)"; then
    fail "schema-drift-unchecked: a drifted record artifact passed the interface-schema non-drift leg"
fi
[[ "$out" == *"schema-drift"* ]] \
    || fail "schema-drift-unchecked: a drifted record artifact did not report schema-drift: $out"
echo "  a drifted record artifact refused: schema-drift"

# The screening half of the same contract: the checked-in consumer
# documents against the record artifacts declaring their shapes —
# deploy/manifest.json against deploy-manifest.schema.json (the
# screening the contract records before the deploy stage's
# rig-agreement check runs) and model/dynamics.json against
# dynamics.schema.json (before the merge's own validation runs).
# ci/schema_conformance.py runs the consumer-side
# required-keys/field-shape conformance and reports schema-mismatch
# itself; two consecutive passes must produce identical digests.
schema_screen() {
    python3 ci/schema_conformance.py \
        --schema "$RECORD/$1" --document "$2" --what "$3"
}
FIRST="$(schema_screen deploy-manifest.schema.json deploy/manifest.json "deployment manifest")"
SECOND="$(schema_screen deploy-manifest.schema.json deploy/manifest.json "deployment manifest")"
[ "$FIRST" = "$SECOND" ] \
    || fail "schema-mismatch-nondeterministic: two manifest-screening passes produced different digests"
echo "  $FIRST"
FIRST="$(schema_screen dynamics.schema.json model/dynamics.json "dynamics document")"
SECOND="$(schema_screen dynamics.schema.json model/dynamics.json "dynamics document")"
[ "$FIRST" = "$SECOND" ] \
    || fail "schema-mismatch-nondeterministic: two dynamics-screening passes produced different digests"
echo "  $FIRST"

# A consumer document violating its declared schema must report the
# diagnostic — doctored copies under the scratch root, so the
# checked-in pair stays pristine.
manifest_case() {
    local doctored="$SCRATCH/manifest-$1.json" out
    python3 - deploy/manifest.json "$doctored" "$1" <<'PY'
import json, sys
document = json.load(open(sys.argv[1]))
case = sys.argv[3]
if case == "missing-required":
    del document["model"]["fingerprint"]
elif case == "mistyped-field":
    document["controllers"][1]["failover_budget"] = "high"
elif case == "mistyped-history-file":
    # The durable-history mount is a path field — a non-string value
    # violates its shape the same way a mistyped journal_file would.
    document["controllers"][0]["history_file"] = 7
elif case == "undeclared-field":
    # The pair's shared tracking secret is a deployment secret the
    # manifest shape deliberately never records.
    document["pair_token"] = "not-for-the-record"
else:
    sys.exit("unknown manifest case " + case)
json.dump(document, open(sys.argv[2], "w"), indent=2)
PY
    if out="$(python3 ci/schema_conformance.py \
            --schema "$RECORD/deploy-manifest.schema.json" \
            --document "$doctored" \
            --what "deployment manifest" 2>&1)"; then
        fail "schema-mismatch-unchecked: the manifest's $1 case passed the schema conformance check"
    fi
    [[ "$out" == *"schema-mismatch"* ]] \
        || fail "schema-mismatch-unchecked: the manifest's $1 case did not report schema-mismatch: $out"
    echo "  manifest $1 refused: schema-mismatch"
}
dynamics_case() {
    local doctored="$SCRATCH/dynamics-$1.json" out
    python3 - model/dynamics.json "$doctored" "$1" <<'PY'
import json, sys
document = json.load(open(sys.argv[1]))
case = sys.argv[3]
if case == "missing-required":
    del document[0]["bool_flow"]["initial"]
elif case == "mistyped-field":
    document[0]["bool_flow"]["input"] = "fifteen"
elif case == "undeclared-element":
    document.append({"not_an_element": {"input": 1, "output": 2}})
else:
    sys.exit("unknown dynamics case " + case)
json.dump(document, open(sys.argv[2], "w"), indent=2)
PY
    if out="$(python3 ci/schema_conformance.py \
            --schema "$RECORD/dynamics.schema.json" \
            --document "$doctored" \
            --what "dynamics document" 2>&1)"; then
        fail "schema-mismatch-unchecked: the dynamics document's $1 case passed the schema conformance check"
    fi
    [[ "$out" == *"schema-mismatch"* ]] \
        || fail "schema-mismatch-unchecked: the dynamics document's $1 case did not report schema-mismatch: $out"
    echo "  dynamics $1 refused: schema-mismatch"
}
for case in missing-required mistyped-field mistyped-history-file \
        undeclared-field; do
    manifest_case "$case"
done
for case in missing-required mistyped-field undeclared-element; do
    dynamics_case "$case"
done

# One `dcs-model diff` leg: $3 is `no changes` — the documents must
# diff clean — or a field the diff listing must name on the element $4
# names. A violated expectation reports diff-mismatch on stderr and
# returns 1; a passing leg prints the listing as the run's evidence.
assert_diff() {
    local old="$1" new="$2" want="$3" element="${4:-}" out
    if ! out="$("$TOOLS/dcs-model" diff "$old" "$new" 2>&1)"; then
        echo "diff-mismatch: dcs-model diff $old $new refused the documents: $out" >&2
        return 1
    fi
    if [ "$want" = "no changes" ]; then
        if [ "$out" != "no changes" ]; then
            echo "diff-mismatch: dcs-model diff $old $new reports differences on identical documents: $out" >&2
            return 1
        fi
    elif ! [[ "$out" == *"$element"* && "$out" == *"$want"* ]]; then
        echo "diff-mismatch: dcs-model diff $old $new does not name $want on $element: $out" >&2
        return 1
    fi
    printf '%s\n' "$out" | sed 's/^/    /'
}

# The doctored compatible revision — the same in-place doctoring the
# upgrade stage applies for the incompatible crossing, but staying
# inside MODEL_VERSION so the diff loads both sides: the primary level
# measurement's declared description changes.
DIFF_REVISED="$SCRATCH/model-revised.json"
python3 - model/plant.json "$DIFF_REVISED" <<'PY'
import json, sys
document = json.load(open(sys.argv[1]))
signal = next(s for s in document["signals"] if s["name"] == "level-primary")
signal["description"] = "Primary wet-well level measurement — revised"
json.dump(document, open(sys.argv[2], "w"), indent=2)
PY
echo "  diff over the doctored compatible revision:"
assert_diff model/plant.json "$DIFF_REVISED" "description:" "signal 10010" || exit 1
echo "  diff over the identical document:"
assert_diff model/plant.json model/plant.json "no changes" || exit 1

# A leg whose expectation fails must report the diagnostic — asserting
# differences on the identical document, asserting none on the doctored
# revision, and diffing a document outside MODEL_VERSION each report
# diff-mismatch.
if out="$(assert_diff model/plant.json model/plant.json "description:" "signal 10010" 2>&1)"; then
    fail "diff-mismatch-unchecked: asserting differences on the identical document passed"
fi
[[ "$out" == *"diff-mismatch"* ]] \
    || fail "diff-mismatch-unchecked: the leg did not report diff-mismatch: $out"
if out="$(assert_diff model/plant.json "$DIFF_REVISED" "no changes" 2>&1)"; then
    fail "diff-mismatch-unchecked: asserting no changes on the doctored revision passed"
fi
[[ "$out" == *"diff-mismatch"* ]] \
    || fail "diff-mismatch-unchecked: the leg did not report diff-mismatch: $out"
DIFF_INVALID="$SCRATCH/model-incompatible.json"
python3 - model/plant.json "$DIFF_INVALID" <<'PY'
import json, sys
document = json.load(open(sys.argv[1]))
document["version"] += 1
json.dump(document, open(sys.argv[2], "w"), indent=2)
PY
if out="$(assert_diff model/plant.json "$DIFF_INVALID" "description:" "signal 10010" 2>&1)"; then
    fail "diff-mismatch-unchecked: diffing a document outside MODEL_VERSION passed"
fi
[[ "$out" == *"diff-mismatch"* ]] \
    || fail "diff-mismatch-unchecked: the leg did not report diff-mismatch: $out"
echo "  failed diff expectations refused: diff-mismatch"

# summary and signal-index over the checked-in model, recorded to the
# run's evidence — the check transcript carries the outputs verbatim
# with their digests.
SUMMARY_OUT="$("$TOOLS/dcs-model" summary model/plant.json)" \
    || fail "tooling-rejected: dcs-model summary refused the checked-in model"
INDEX_OUT="$("$TOOLS/dcs-model" signal-index model/plant.json)" \
    || fail "tooling-rejected: dcs-model signal-index refused the checked-in model"
echo "  dcs-model summary (sha256 $(printf '%s\n' "$SUMMARY_OUT" | sha256sum | cut -d' ' -f1)):"
printf '%s\n' "$SUMMARY_OUT" | sed 's/^/    /'
echo "  dcs-model signal-index (sha256 $(printf '%s\n' "$INDEX_OUT" | sha256sum | cut -d' ' -f1)):"
printf '%s\n' "$INDEX_OUT" | sed 's/^/    /'

echo "== alarm-validation =="
# The rejection half of decision 70's alarm record — a leg beside the
# acceptance checks above: ci/alarm_validation.py audits the emitted
# document's managed-alarm record (each instance's rationalization
# prose and priority/class/response_ticks codes), doctors copies of it
# — one managed alarm's required_action removed, the same field
# emptied, another kind's priority removed — and requires the released
# `dcs-controller --check` to refuse each naming the missing element,
# never a silent load, then checks the driven run's served
# components/parameters sections report the same record. Contract
# violations report `alarm-validation: …` lines on stderr.
run_alarm_validation() {
    python3 ci/alarm_validation.py \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json
}
ALARM_1="$(run_alarm_validation)" \
    || fail "alarm-validation-failed: the alarm-validation leg refused"
ALARM_2="$(run_alarm_validation)" \
    || fail "alarm-validation-failed: the alarm-validation leg refused"
[ "$ALARM_1" = "$ALARM_2" ] \
    || fail "alarm-validation-nondeterministic: two alarm-validation passes produced different digests"
echo "  $ALARM_1"

# The leg's refusal assertions must themselves be proven — a run that
# writes the pristine document where each doctored copy belongs must
# report every acceptance, never pass a silent load.
if out="$(python3 ci/alarm_validation.py \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --tamper skip-doctoring 2>&1)"; then
    fail "alarm-validation-unchecked: a run with the doctoring skipped passed"
fi
[[ "$out" == *"alarm-validation:"* ]] \
    || fail "alarm-validation-unchecked: the skipped-doctoring run did not report alarm-validation: $out"
echo "  a skipped-doctoring run refused: alarm-validation"

echo "== fingerprint =="
EMITTED_FP="$("$BIN" --fingerprint)"
MANIFEST_FP="$(python3 -c 'import json; print(json.load(open("deploy/manifest.json"))["model"]["fingerprint"])')"
[ "$EMITTED_FP" = "$MANIFEST_FP" ] \
    || fail "manifest-fingerprint-mismatch: emitted model fingerprints $EMITTED_FP but deploy/manifest.json records $MANIFEST_FP"
echo "  fingerprint $EMITTED_FP matches the manifest"

# The served-bytes half of the model authorization: the recorded
# fingerprint must name what the deployed pair actually serves, not
# just the checked-in artifact — ci/fingerprint.py launches the
# manifest-declared pair on the released tooling and pulls each peer's
# stamped model digest through GET /checkpoint, reporting the named
# manifest-fingerprint-mismatch with the expected vs served fingerprint
# and the first diverging document section on any divergence. Two
# passes must produce identical digests.
run_fingerprint() {
    python3 ci/fingerprint.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_fingerprint)" \
    || fail "fingerprint-failed: the manifest-fingerprint leg did not hold — its evidence lines are above"
SECOND="$(run_fingerprint)" \
    || fail "fingerprint-failed: the manifest-fingerprint leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "fingerprint-nondeterministic: two fingerprint-leg passes produced different digests"
echo "  $FIRST"

# The doctored case: the pair serving a model whose point ids were
# renumbered — identical component content — must surface the named
# diagnostic; a silent pass would leave the authorization unproven.
if out="$(run_fingerprint --tamper renumber-points 2>&1)"; then
    fail "fingerprint-unchecked: a served model with renumbered point ids passed the fingerprint leg"
fi
[[ "$out" == *"manifest-fingerprint-mismatch"* && "$out" == *"io_points"* ]] \
    || fail "fingerprint-unchecked: the renumber-points case did not report its named diagnostic: $out"
echo "  renumber-points: reported, manifest-fingerprint-mismatch"

# The dynamics half of the authorization, under the same canonical
# fingerprint contract: the optional `dynamics.fingerprint` must name
# the dynamics document the deployment serves — the manifest's
# declared `dynamics.path`, the read-only mount the rig instantiates
# and `dcs-plant-server --dynamics` merges — and the checked-in
# artifact must fingerprint it identically. A manifest omitting the
# optional field declares no pin; the served-vs-checked-in comparison
# still stands.
DYN_FP="$(python3 ci/dynamics_fingerprint.py --fingerprint model/dynamics.json)"
MANIFEST_DYN_FP="$(python3 -c 'import json; print(json.load(open("deploy/manifest.json")).get("dynamics", {}).get("fingerprint") or "")')"
if [ -n "$MANIFEST_DYN_FP" ]; then
    [ "$DYN_FP" = "$MANIFEST_DYN_FP" ] \
        || fail "manifest-fingerprint-mismatch: the checked-in dynamics fingerprints $DYN_FP but deploy/manifest.json records $MANIFEST_DYN_FP"
    echo "  dynamics fingerprint $DYN_FP matches the manifest"
else
    echo "  the manifest records no dynamics.fingerprint — the pin is undeclared"
fi

# The served-bytes half: ci/dynamics_fingerprint.py launches the
# manifest-declared pair on the released tooling serving the
# deployment's declared dynamics.path, fingerprints the document it
# actually runs, and holds it equal to the recorded fingerprint and
# the checked-in artifact — reporting manifest-fingerprint-mismatch
# with the expected vs served fingerprint and the first diverging
# element on any divergence. Two passes must produce identical
# digests.
run_dynamics_fingerprint() {
    python3 ci/dynamics_fingerprint.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_dynamics_fingerprint)" \
    || fail "dynamics-fingerprint-failed: the dynamics-fingerprint leg did not hold — its evidence lines are above"
SECOND="$(run_dynamics_fingerprint)" \
    || fail "dynamics-fingerprint-failed: the dynamics-fingerprint leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "dynamics-fingerprint-nondeterministic: two dynamics-fingerprint passes produced different digests"
echo "  $FIRST"

# The doctored case: the pair serving a dynamics document whose point
# references were renumbered — identical element content — must
# surface the named diagnostic; a silent pass would leave the
# authorization unproven.
if out="$(run_dynamics_fingerprint --tamper renumber-points 2>&1)"; then
    fail "dynamics-fingerprint-unchecked: a served dynamics document with renumbered points passed the fingerprint leg"
fi
[[ "$out" == *"manifest-fingerprint-mismatch"* && "$out" == *"element 0"* ]] \
    || fail "dynamics-fingerprint-unchecked: the renumber-points case did not report its named diagnostic: $out"
echo "  renumber-points: reported, manifest-fingerprint-mismatch"

echo "== deploy =="
# The rig-definition consistency check reports its own named
# diagnostics (rig-invalid, rig-unverifiable, rig-mismatch) on stderr.
python3 ci/deploy_rig.py

# The overview-URL generator's rig-resolution half — ci/overview_url.py
# reads the manifest's declared topology and resolves each declared
# member to the monitor endpoint the rig definition publishes, printing
# the deployment's ?pair= overview URL (the pair stage's pair-overview
# leg asserts the generated names and serving endpoints against the
# launched pair). It reports its own `overview-url: …` diagnostics —
# a declared member with no published endpoint fails by name.
python3 ci/overview_url.py

# The persistence fields' divergence cases, exercised against doctored
# scratch copies so the checked-in pair stays pristine: each must
# report rig-mismatch — a declared path missing its mount or flag, a
# flag or writable mount the manifest does not declare, a persistence
# mount left read-only, one controller's journal_file aliased with its
# state_file — while the fields omitted outright (with their
# mounts and flags) stay a valid deployment. The same harness proves
# the checked-in manifest's declared topology section — the deployed
# pair named under `topology.pairs`: the declaration validates under
# another pair name, while a member the rig does not declare, a
# member two pairs share, a pair whose standby edge leaves it, a
# pair carrying two standby declarations, or a declared pair whose
# standby wiring does not close inside it each report rig-mismatch —
# and the one-field-per-deployment bound (decision 99) refuses the
# planted undeployable shapes: a second declared pair over the
# manifest's one plant and a second duty controller the section
# never names, each a second field-owning claimant on the
# single-writer claim.
RIG_DIR="$(mktemp -d)"
mkdir -p "$RIG_DIR/deploy" "$RIG_DIR/ci" "$RIG_DIR/model"
cp deploy/manifest.json deploy/compose.yaml "$RIG_DIR/deploy/"
cp ci/deploy_rig.py "$RIG_DIR/ci/"
cp model/plant.json model/dynamics.json "$RIG_DIR/model/"
cp "$RIG_DIR/deploy/compose.yaml" "$RIG_DIR/compose.pristine.yaml"
cp "$RIG_DIR/deploy/manifest.json" "$RIG_DIR/manifest.pristine.json"

rig_case() {
    cp "$RIG_DIR/compose.pristine.yaml" "$RIG_DIR/deploy/compose.yaml"
    cp "$RIG_DIR/manifest.pristine.json" "$RIG_DIR/deploy/manifest.json"
    python3 - "$RIG_DIR" "$1" <<'PY'
import json
import sys

root, case = sys.argv[1], sys.argv[2]
compose_path = root + "/deploy/compose.yaml"
manifest_path = root + "/deploy/manifest.json"
compose = open(compose_path).read()
manifest = open(manifest_path).read()
if case == "persistence-mount-divergence":
    # ctrl-a's writable volume moves off the declared paths.
    compose = compose.replace(
        "ctrl-a-data:/var/tmp", "ctrl-a-data:/srv/other", 1)
elif case == "persistence-flag-divergence":
    # ctrl-a's --journal-file argument diverges from the manifest.
    compose = compose.replace(
        "- /var/tmp/journal.jsonl", "- /var/tmp/other.jsonl", 1)
elif case == "persistence-history-flag-divergence":
    # ctrl-a's --history-file argument diverges from the manifest.
    compose = compose.replace(
        "- /var/tmp/history.jsonl", "- /var/tmp/other.jsonl", 1)
elif case == "persistence-history-flag-missing":
    # ctrl-a keeps its declared history_file while the rig
    # definition drops the --history-file flag.
    compose = compose.replace(
        "      - --history-file\n      - /var/tmp/history.jsonl\n", "", 1)
elif case == "persistence-mount-read-only":
    compose = compose.replace(
        "ctrl-a-data:/var/tmp", "ctrl-a-data:/var/tmp:ro", 1)
elif case == "undeclared-persistence-flag":
    # ctrl-a keeps its --journal-file while the manifest drops the
    # field — an undeclared flag.
    document = json.loads(manifest)
    del document["controllers"][0]["journal_file"]
    manifest = json.dumps(document, indent=2)
elif case == "undeclared-history-flag":
    # ctrl-a keeps its --history-file while the manifest drops the
    # field — an undeclared flag.
    document = json.loads(manifest)
    del document["controllers"][0]["history_file"]
    manifest = json.dumps(document, indent=2)
elif case == "undeclared-writable-mount":
    # ctrl-a gains writable storage the manifest declares nothing
    # under.
    compose = compose.replace(
        "      - ctrl-a-data:/var/tmp",
        "      - ctrl-a-data:/var/tmp\n      - ctrl-a-scratch:/scratch",
        1,
    )
    compose = compose.replace(
        "volumes:\n  ctrl-a-data:",
        "volumes:\n  ctrl-a-data:\n  ctrl-a-scratch:",
        1,
    )
elif case == "failover-flag-missing":
    # ctrl-b keeps its declared failover_budget while the rig
    # definition drops the --auto-promote flag.
    compose = compose.replace(
        "      - --auto-promote\n      - \"3\"\n", "", 1)
elif case == "failover-flag-undeclared":
    # The rig definition keeps --auto-promote while the manifest
    # drops the declaration — an undeclared flag.
    document = json.loads(manifest)
    del document["controllers"][1]["failover_budget"]
    manifest = json.dumps(document, indent=2)
elif case == "failover-wrong-peer":
    # The declared budget moves to the duty entry — automatic
    # failover arms a tracking standby only.
    document = json.loads(manifest)
    document["controllers"][0]["failover_budget"] = \
        document["controllers"][1].pop("failover_budget")
    manifest = json.dumps(document, indent=2)
elif case == "persistence-aliased-paths":
    # ctrl-a's journal_file aliases its state_file — the checkpoint's
    # write-then-rename would orphan the append writer's descriptor
    # (finding state-file-alias-clobbers-append-durable-files). The
    # flag follows the field so flag/field parity still holds and the
    # only divergence is the alias itself.
    document = json.loads(manifest)
    document["controllers"][0]["journal_file"] = "/var/tmp/state.json"
    manifest = json.dumps(document, indent=2)
    compose = compose.replace(
        "- /var/tmp/journal.jsonl", "- /var/tmp/state.json", 1)
elif case == "persistence-omitted":
    # All three fields omitted together with their flags and mounts —
    # the optional deployment a consumer without durable storage
    # declares.
    document = json.loads(manifest)
    for controller in document["controllers"]:
        controller.pop("state_file", None)
        controller.pop("journal_file", None)
        controller.pop("history_file", None)
    manifest = json.dumps(document, indent=2)
    for line in (
        "      - ctrl-a-data:/var/tmp\n",
        "      - ctrl-b-data:/var/tmp\n",
        "      - --state-file\n      - /var/tmp/state.json\n",
        "      - --journal-file\n      - /var/tmp/journal.jsonl\n",
        "      - --history-file\n      - /var/tmp/history.jsonl\n",
    ):
        compose = compose.replace(line, "")
elif case == "topology-declared":
    # The checked-in manifest declares the deployed pair; the
    # declaration stays valid under another pair name — the name is
    # the deployment's free index entry, the membership and the
    # wiring what must close.
    document = json.loads(manifest)
    document["topology"] = {
        "pairs": [{"name": "pump-pair", "members": ["ctrl-a", "ctrl-b"]}]
    }
    manifest = json.dumps(document, indent=2)
elif case == "topology-multi-pair":
    # Two named pairs over a four-controller rig on the manifest's
    # one plant — the planted undeployable topology decision 99
    # rules out: each pair needs a duty member, and two duty
    # claimants cannot both hold the field's single-writer claim.
    # The rig grows the matching second pair's services and volumes,
    # cloned from the first pair's blocks on fresh names and ports.
    document = json.loads(manifest)
    document["controllers"] += [
        {
            "name": "ctrl-c",
            "listen": "0.0.0.0:8082",
            "state_file": "/var/tmp/state.json",
            "journal_file": "/var/tmp/journal.jsonl",
            "history_file": "/var/tmp/history.jsonl",
        },
        {
            "name": "ctrl-d",
            "listen": "0.0.0.0:8083",
            "standby": "ctrl-c:8082",
            "state_file": "/var/tmp/state.json",
            "journal_file": "/var/tmp/journal.jsonl",
            "history_file": "/var/tmp/history.jsonl",
        },
    ]
    document["topology"] = {
        "pairs": [
            {"name": "station-a", "members": ["ctrl-a", "ctrl-b"]},
            {"name": "station-b", "members": ["ctrl-c", "ctrl-d"]},
        ]
    }
    manifest = json.dumps(document, indent=2)
    block_a = compose[
        compose.index("  ctrl-a:"):compose.index("  ctrl-b:")
    ]
    block_b = compose[compose.index("  ctrl-b:"):compose.index("\nnetworks:")]
    block_c = block_a.replace("ctrl-a", "ctrl-c").replace("8080", "8082")
    block_d = (
        block_b.replace("ctrl-b", "ctrl-d")
        .replace("ctrl-a:8080", "ctrl-c:8082")
        .replace("ctrl-a:", "ctrl-c:")
        .replace("8081", "8083")
        .replace('      - --auto-promote\n      - "3"\n', "")
    )
    compose = compose.replace(
        "\nnetworks:",
        "\n" + block_c + "\n" + block_d + "\nnetworks:",
        1,
    )
    compose = compose.replace(
        "  ctrl-a-data:\n  ctrl-b-data:\n",
        "  ctrl-a-data:\n  ctrl-b-data:\n  ctrl-c-data:\n  ctrl-d-data:\n",
        1,
    )
elif case == "undeployable-second-duty":
    # The same undeployable shape with no topology section at all:
    # a second duty controller — an entry without `standby` — is a
    # second field-owning claimant on the manifest's one plant.
    document = json.loads(manifest)
    document["controllers"].append(
        {
            "name": "ctrl-c",
            "listen": "0.0.0.0:8082",
            "state_file": "/var/tmp/state.json",
            "journal_file": "/var/tmp/journal.jsonl",
            "history_file": "/var/tmp/history.jsonl",
        }
    )
    manifest = json.dumps(document, indent=2)
    block_c = compose[
        compose.index("  ctrl-a:"):compose.index("  ctrl-b:")
    ].replace("ctrl-a", "ctrl-c").replace("8080", "8082")
    compose = compose.replace(
        "\nnetworks:", "\n" + block_c + "\nnetworks:", 1
    )
    compose = compose.replace(
        "  ctrl-a-data:\n  ctrl-b-data:\n",
        "  ctrl-a-data:\n  ctrl-b-data:\n  ctrl-c-data:\n",
        1,
    )
elif case == "topology-undeclared-member":
    # A named pair member the rig does not declare.
    document = json.loads(manifest)
    document["topology"] = {
        "pairs": [{"name": "station", "members": ["ctrl-a", "ctrl-z"]}]
    }
    manifest = json.dumps(document, indent=2)
elif case == "topology-shared-member":
    # Two named pairs claiming the same member.
    document = json.loads(manifest)
    document["topology"] = {
        "pairs": [
            {"name": "station-a", "members": ["ctrl-a", "ctrl-b"]},
            {"name": "station-b", "members": ["ctrl-a", "ctrl-b"]},
        ]
    }
    manifest = json.dumps(document, indent=2)
elif case == "topology-external-standby":
    # The declared pair's standby edge leaves it: the pair's tracking
    # member names a peer outside the pair — wiring that does not
    # close inside the declaration.
    document = json.loads(manifest)
    document["controllers"][1]["standby"] = "ctrl-c:8082"
    manifest = json.dumps(document, indent=2)
elif case == "topology-two-standbys":
    # Both members declare standby: a pair is one duty controller
    # tracked by one standby, and the second declaration diverges.
    document = json.loads(manifest)
    document["controllers"][0]["standby"] = "ctrl-b:8081"
    manifest = json.dumps(document, indent=2)
elif case == "topology-unwired-pair":
    # The declared pair's wiring does not close inside it: dropping
    # the standby field and flag leaves two duty controllers the
    # section still calls a pair.
    document = json.loads(manifest)
    document["topology"] = {
        "pairs": [{"name": "station", "members": ["ctrl-a", "ctrl-b"]}]
    }
    del document["controllers"][1]["standby"]
    manifest = json.dumps(document, indent=2)
    compose = compose.replace(
        "      - --standby\n      - ctrl-a:8080\n", "", 1)
else:
    sys.exit("unknown rig case " + case)
open(compose_path, "w").write(compose)
open(manifest_path, "w").write(manifest)
PY
    local out
    if out="$(cd "$RIG_DIR" && python3 ci/deploy_rig.py 2>&1)"; then
        case "$1" in
            persistence-omitted|topology-declared)
                echo "  $1: optional declaration — the manifest and the rig agree"
                return
                ;;
        esac
        fail "rig-mismatch-unchecked: the $1 divergence passed the rig check"
    fi
    case "$1" in
        persistence-omitted|topology-declared)
            fail "rig-mismatch-unchecked: the $1 case reported: $out"
            ;;
    esac
    [[ "$out" == *"rig-mismatch"* ]] \
        || fail "rig-mismatch-unchecked: the $1 divergence did not report rig-mismatch: $out"
    echo "  $1 refused: rig-mismatch"
}

for divergence in persistence-mount-divergence persistence-flag-divergence \
        persistence-history-flag-divergence \
        persistence-history-flag-missing \
        persistence-mount-read-only undeclared-persistence-flag \
        undeclared-history-flag \
        undeclared-writable-mount failover-flag-missing \
        failover-flag-undeclared failover-wrong-peer \
        persistence-aliased-paths persistence-omitted \
        topology-declared topology-multi-pair \
        undeployable-second-duty topology-undeclared-member \
        topology-shared-member topology-external-standby \
        topology-two-standbys topology-unwired-pair; do
    rig_case "$divergence"
done

echo "== simulate =="
run_simulation() {
    python3 ci/simulate.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json
}
FIRST="$(run_simulation)" || fail "scenario-failed: the scripted simulation's declared outcomes did not hold"
SECOND="$(run_simulation)" || fail "scenario-failed: the scripted simulation's declared outcomes did not hold"
[ "$FIRST" = "$SECOND" ] \
    || fail "scenario-nondeterministic: two simulation runs produced different digests"
echo "  $FIRST"

echo "== restart =="
# The lone-controller half of WW-LCM-001's restart-recovery evidence:
# the field-owning controller launches with the manifest-declared
# --state-file/--journal-file flags at runner-owned scratch paths,
# runs the deterministic scenario to a leg boundary — far enough to
# leave applied receipts and journaled transitions — is stopped, and
# relaunches onto the same files. The resumed run must continue at the
# persisted tick, its leg outcomes, receipts, and served field image
# equal to an uninterrupted reference pass, the journal's seq order
# continuing across the file's run-boundary marker. Two passes must
# produce identical digests.
run_restart() {
    python3 ci/restart.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_restart)" \
    || fail "restart-resume-failed: the restart leg did not hold — its evidence lines are above"
SECOND="$(run_restart)" \
    || fail "restart-resume-failed: the restart leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "restart-resume-nondeterministic: two restart-leg passes produced different digests"
echo "  $FIRST"

# The doctored cases: each tamper at the restart point must surface
# the named diagnostic — never a silently accepted tick-zero restart.
# A corrupt state file is refused by the relaunch's startup naming the
# file; a missing one cold-starts the resumed run at tick zero, which
# the leg's own checks catch and report.
for tamper in missing-state-file corrupt-state-file; do
    if out="$(run_restart --tamper "$tamper" 2>&1)"; then
        fail "restart-resume-unchecked: a $tamper passed the restart leg"
    fi
    case "$tamper" in
        missing-state-file) evidence="never reported a resume" ;;
        corrupt-state-file) evidence="does not hold a checkpoint" ;;
    esac
    [[ "$out" == *"$evidence"* ]] \
        || fail "restart-resume-unchecked: the $tamper case did not report its named diagnostic: $out"
    echo "  $tamper: reported, restart-resume-failed"
done

echo "== surface =="
python3 ci/simulate.py \
    --surface \
    --schema-out "$SCRATCH/served-schema.json" \
    --plant-server "$TOOLS/dcs-plant-server" \
    --controller "$TOOLS/dcs-controller" \
    --model model/plant.json \
    --dynamics model/dynamics.json \
    --scenario ci/scenario.json \
    || fail "surface-mismatch: the served operator surface — signal index, page, descriptors, interface registry, declared commands, their availability verdicts, emitted events — does not match the emitted model's declared surface"

# The served GET /schema document against the fetched record artifact's
# declared structure — the consumer-side required-keys/field-shape
# conformance a non-Rust consumer runs (README §5 documents the
# boundary: full draft-2020-12 validation stays workspace-side).
# ci/schema_conformance.py reports schema-mismatch itself.
python3 ci/schema_conformance.py \
    --schema "$RECORD/block-interfaces.schema.json" \
    --document "$SCRATCH/served-schema.json"

# A served document missing or mistyping a required field must report
# the diagnostic — doctored copies, so the recorded document stays
# pristine.
served_case() {
    local doctored="$SCRATCH/served-$1.json" out
    python3 - "$SCRATCH/served-schema.json" "$doctored" "$1" <<'PY'
import json, sys
document = json.load(open(sys.argv[1]))
case = sys.argv[3]
if case == "missing-required":
    del document["tick"]
elif case == "mistyped-required":
    document["tick"] = "not-a-tick"
elif case == "mistyped-nested":
    document["interfaces"][0]["interface"]["version"] = "1"
else:
    sys.exit("unknown served case " + case)
json.dump(document, open(sys.argv[2], "w"), indent=2)
PY
    if out="$(python3 ci/schema_conformance.py \
            --schema "$RECORD/block-interfaces.schema.json" \
            --document "$doctored" 2>&1)"; then
        fail "schema-mismatch-unchecked: the $1 case passed the served-schema conformance check"
    fi
    [[ "$out" == *"schema-mismatch"* ]] \
        || fail "schema-mismatch-unchecked: the $1 case did not report schema-mismatch: $out"
    echo "  $1 refused: schema-mismatch"
}
for case in missing-required mistyped-required mistyped-nested; do
    served_case "$case"
done

echo "== pair =="
# The redundant-pair half of WW-LCM-001's switchover evidence — the
# deployment the manifest declares, run: the deploy stage proves the
# wiring statically; the stage's legs run it. Each leg is one file
# under ci/legs/ — self-registering through its module-level LEG
# literal, its explanatory comment carried in its own docstring —
# discovered and driven by ci/legs.py in the recorded order: each leg
# runs twice requiring identical digests, then each declared doctored
# case runs requiring its named evidence. The legs share the
# launch/settle/restore harness consolidated under #647 —
# ci/legs/pair.py's launch_pair/PairRig — and each restores the pair's
# launch roles for the next. Adding a leg is one new file under
# ci/legs/ — nothing in this script changes. The driver reports each
# leg's named diagnostics itself.
# The legs' upgrade_from half: a leg declaring `upgrade_tools` runs
# its flag arguments against the recorded upgrade-from revision's
# tooling — the same `cargo install` resolution the pinned release's
# went through, here at $DCS_UPGRADE_REV (the upgrade stage's recorded
# baseline). Under a `DCS_TOOLS` substitution the directory resolves
# to the substituted set, so the leg rolls the substituted binaries.
PINNED_TOOLS="$TOOLS"
ensure_tools "$DCS_UPGRADE_REV" \
    || fail "pin-unresolvable: cargo install --git $DCS_REMOTE --rev $DCS_UPGRADE_REV failed"
UPGRADE_TOOLS="$TOOLS"
TOOLS="$PINNED_TOOLS"
TOOLS_REV="$DCS_REV"
python3 ci/legs.py \
    --plant-server "$TOOLS/dcs-plant-server" \
    --controller "$TOOLS/dcs-controller" \
    --model model/plant.json \
    --dynamics model/dynamics.json \
    --scenario ci/scenario.json \
    --manifest deploy/manifest.json \
    --tools "$TOOLS" \
    --upgrade-tools "$UPGRADE_TOOLS" || exit 1

echo "== consumers =="
# The boundary lint half, alongside the lockfile stage's rule: the
# stage's driver and the README's consumer obligations name only
# released artifacts and documented endpoints — never a path into a
# platform checkout.
# The ci/legs/*.py glob is the directory rule covering the pair
# stage's leg convention — a new leg registers by file and needs no
# edit here.
for file in ci/alarm_rationalization.py ci/alarm_validation.py \
        ci/claim_fencing.py ci/consumers.py ci/ctl.py ci/deploy_rig.py \
        ci/dynamics_fingerprint.py ci/fingerprint.py ci/legs.py \
        ci/managed_carryover.py ci/oos.py ci/overview_url.py \
        ci/power_trip.py \
        ci/restart.py ci/schema_conformance.py ci/simulate.py \
        ci/staging.py ci/legs/*.py README.md; do
    if grep -nE 'crates/|\.\./|file://|/home/|target/debug' "$file"; then
        fail "path-dependency-leak: $file references a platform-checkout path"
    fi
done
# The behavioral half: the simulate stage's deterministic driven run
# replays once per consumer schedule. Identical digests across the
# schedules prove no consumer behavior — absent, polling, stalled,
# churning, malformed, or restarted — can change an output or a
# receipt; identical digests across two passes prove the stage itself
# is deterministic.
run_consumer_schedules() {
    local reference="" out digest
    for schedule in zero-clients polling stalled-reader \
            disconnect-reconnect malformed-and-flood ui-restart; do
        out="$(python3 ci/consumers.py \
            --plant-server "$TOOLS/dcs-plant-server" \
            --controller "$TOOLS/dcs-controller" \
            --model model/plant.json \
            --dynamics model/dynamics.json \
            --scenario ci/scenario.json \
            --schedule "$schedule")" \
            || fail "consumer-interference: the $schedule schedule did not hold — its evidence lines are above"
        digest="$(printf '%s\n' "$out" | sed -n 's/^consumer-digest //p')"
        [ -n "$digest" ] \
            || fail "consumer-interference: the $schedule schedule reported no digest"
        if [ -z "$reference" ]; then
            reference="$digest"
        elif [ "$digest" != "$reference" ]; then
            fail "consumer-interference: the $schedule schedule changed the run's outputs and receipts (digest $digest, reference $reference)"
        fi
        echo "  $schedule: consumer-digest $digest" >&2
    done
    echo "$reference"
}
FIRST="$(run_consumer_schedules)" || exit 1
SECOND="$(run_consumer_schedules)" || exit 1
[ "$FIRST" = "$SECOND" ] \
    || fail "consumer-nondeterministic: two consumer-stage passes produced different digests"
echo "  consumer-digest $FIRST identical across every schedule and both passes"

echo "== ctl =="
# The shipped operator CLI over the simulate stage's driven run: the
# leg drives scans through `dcs-ctl scan`, exercises the sequencer's
# kind-declared commands through `dcs-ctl invoke` and reads the served
# contract through the remaining subcommands — the release set's
# documented operator surface proven from the released binary alone.
run_ctl() {
    python3 ci/ctl.py \
        --ctl "$TOOLS/dcs-ctl" \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        || fail "ctl-failed: the dcs-ctl leg did not hold — its evidence lines are above"
}
FIRST="$(run_ctl)" || exit 1
SECOND="$(run_ctl)" || exit 1
[ "$FIRST" = "$SECOND" ] \
    || fail "ctl-nondeterministic: two ctl-stage passes produced different digests"
echo "  $FIRST identical across both passes"

if [ "${DCS_UPGRADE:-1}" != "0" ]; then

echo "== upgrade =="
# README §7's customer path exercised against this repository's own
# composition: materialize the tree pinned at the previous release's
# recorded rev — the release this tree upgraded from — repin to the
# recorded release rev, move the lockfile, and re-run the check — a
# compatible crossing is a drop-in upgrade, so the emitted bytes must
# not change (emit-divergent). The copy keeps the working tree
# untouched.
UPGRADE_DIR="$(mktemp -d)"
for path in Cargo.toml Cargo.lock rust-toolchain.toml README.md src model deploy ci; do
    cp -r "$path" "$UPGRADE_DIR/"
done
export CARGO_TARGET_DIR="$UPGRADE_DIR/target"

# The baseline: the composition as the previous release's recorded rev
# emits it — the pinned side of the crossing this tree already ran.
repin "rev = \"$DCS_UPGRADE_REV\""
( cd "$UPGRADE_DIR" && cargo fetch ) \
    || fail "pin-unresolvable: the upgrade-from revision $DCS_UPGRADE_REV did not resolve"
( cd "$UPGRADE_DIR" && cargo build --quiet ) \
    || fail "surface-incompatible: the composition does not compile against the upgrade-from revision"
UPGRADE_BIN="$UPGRADE_DIR/target/debug/pump-station"
"$UPGRADE_BIN" > "$UPGRADE_DIR/emit-released.json"
cmp -s "$UPGRADE_DIR/emit-released.json" model/plant.json \
    || fail "emit-divergent: the previous release's recorded rev emits different bytes than the approved model/plant.json"

# The repin to the recorded release: only the pin changes — src/,
# deploy/, and model/ are the unchanged tree. The fetch re-resolves
# and moves the copied lockfile, README §7's `cargo update` step.
repin "rev = \"$DCS_REV\""
( cd "$UPGRADE_DIR" && cargo fetch ) \
    || fail "pin-unresolvable: the recorded release rev $DCS_REV did not resolve"
( cd "$UPGRADE_DIR" && cargo build --quiet ) \
    || fail "surface-incompatible: the composition does not compile against the recorded release rev"
"$UPGRADE_BIN" > "$UPGRADE_DIR/emit-upgraded.json"
cmp -s "$UPGRADE_DIR/emit-upgraded.json" model/plant.json \
    || fail "emit-divergent: the unchanged composition emitted different model bytes under $DCS_REV"
echo "  byte-identical emit across the repin $DCS_UPGRADE_REV -> $DCS_REV"

# The full pipeline under the repin — this check's own stages re-run
# against the repinned materialization, with the release tooling
# resolved at the recorded release rev.
ensure_tools "$DCS_REV" \
    || fail "pin-unresolvable: cargo install --git $DCS_REMOTE --rev $DCS_REV failed"
(
    cd "$UPGRADE_DIR"
    DCS_UPGRADE=0 DCS_REMOTE="$DCS_REMOTE" DCS_REV="$DCS_REV" \
        DCS_TOOLS="$TOOLS" bash ci/check.sh
) || { echo "the repinned pipeline failed — its named diagnostic is above" >&2; exit 1; }
echo "  the full pipeline passes under the repin"

# The named incompatible crossings, against this tree: each must be
# refused — a crossing that resolves is the contract's
# crossing-unrefused.
expect_pin_refused() {
    repin "$1"
    local out
    if out="$( cd "$UPGRADE_DIR" && cargo fetch 2>&1 )"; then
        fail "crossing-unrefused: pin \`$1\` resolved — the incompatible crossing must be refused"
    fi
    echo "  $2 refused: pin-unresolvable"
    echo "$out" | tail -n 1 | sed 's/^/    /'
}
expect_pin_refused 'tag = "no-such-release"' "the unresolvable tag \`no-such-release\`"
expect_pin_refused "rev = \"$DCS_UPGRADE_REV\", version = \">=99\"" \
    "the pin outside the supported version window"

# A document outside MODEL_VERSION is refused by the released tooling.
DOCTORED="$UPGRADE_DIR/model-doctored.json"
python3 - model/plant.json "$DOCTORED" <<'PY'
import json, sys
document = json.load(open(sys.argv[1]))
document["version"] += 1
json.dump(document, open(sys.argv[2], "w"), indent=2)
PY
if "$TOOLS/dcs-model" validate "$DOCTORED" >/dev/null 2>&1; then
    fail "crossing-unrefused: dcs-model validate accepted a document outside MODEL_VERSION"
fi
echo "  a document outside MODEL_VERSION is refused"

fi
echo "check ok"
