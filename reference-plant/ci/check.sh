#!/usr/bin/env bash
# The reference plant's clean-CI check: everything a fresh clone of this
# repository runs with no platform checkout present.
#
# Stages, each reporting the release contract's named diagnostics on
# failure (docs/release-contract.md):
#
#   resolve      cargo fetch — the pinned release crates resolve
#                (pin-unresolvable)
#   build        cargo build — the composition compiles against the
#                supported surface (surface-incompatible)
#   lockfile     Cargo.lock records only git sources for the release
#                crates — never a path into a checkout
#                (path-dependency-leak)
#   emit         the model and scenario emit byte-identically twice and
#                match the checked-in artifacts (emit-nondeterministic,
#                stale-artifact)
#   tooling      the released tooling accepts the emitted model —
#                `dcs-model validate`, `dcs-model lint`,
#                `dcs-controller --check` — and exercises the contract's
#                remaining dcs-model surfaces: `dcs-model schema` and
#                `dcs-model interface-schema` emissions byte-identical
#                to the release record's schema artifacts (fetched from
#                the pinned revision through the same git remote the
#                pins resolve over), `dcs-model diff` naming a doctored
#                compatible revision's changes and none on the
#                identical document, and `dcs-model summary` /
#                `dcs-model signal-index` outputs recorded to the run's
#                evidence (tooling-rejected, pin-unresolvable,
#                schema-drift, diff-mismatch)
#   fingerprint  the emitted model's fingerprint equals the manifest's
#                recorded `model.fingerprint`
#                (manifest-fingerprint-mismatch)
#   deploy       the checked-in rig definition deploy/compose.yaml
#                instantiates every field of deploy/manifest.json —
#                release, images, mounted model and dynamics paths,
#                the fingerprint propagated into the controller
#                invocations, listen addresses, the pair's standby
#                wiring, and the optional per-controller persistence
#                paths (state_file/journal_file) backed by writable
#                mounts and flags — parsed and validated through
#                `docker compose config` or the fallback parser, with
#                the fields' divergence cases exercised against
#                doctored copies
#                (rig-invalid, rig-unverifiable, rig-mismatch)
#   simulate     the scripted simulation's declared outcomes hold, and
#                two runs produce identical digests (scenario-failed,
#                scenario-nondeterministic)
#   restart      the restart-recovery leg (WW-LCM-001's
#                lone-controller clause): the field-owning controller
#                runs the deterministic scenario on the
#                manifest-declared --state-file/--journal-file flags
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
#                structured receipts through POST /command, and the
#                kind-emitted events reaching the journal and the
#                per-instance resource view — matches the emitted
#                model's declaration, in the same deterministic
#                --driven run the simulate stage performs — plus the
#                served GET /schema document's structural conformance
#                to the fetched block-interfaces schema artifact
#                (surface-mismatch, schema-mismatch)
#   pair         the consumer-declared redundant pair — the deployment
#                the manifest actually declares, not just its
#                definition: ci/pair.py reads the standby wiring and
#                persistence fields out of deploy/manifest.json and
#                spawns dcs-plant-server plus two released
#                dcs-controller --driven --remote instances wired per
#                the manifest, the declared --state-file/--journal-file
#                paths under a runner-owned scratch directory. The
#                standby converges to tracking through the served
#                GET /role, scans driven through POST /scan keep the
#                peers' images identical, a receipted demote/promote
#                switches the roles, and the run continues bumplessly
#                with the adopted receipts and the durable journal
#                files' transition records intact; two passes produce
#                identical digests (pair-failed, pair-nondeterministic).
#                The stage's refusal half, ci/refusal.py on the same
#                declared deployment: a POST /promote on the freshly
#                launched standby — before its first transfer — answers
#                the named not_converged refusal with no field
#                hand-off, a receipted write against a declared
#                writable point submitted to the tracking standby's
#                monitor answers the named not_active rejection with
#                the point unchanged in the active's served snapshot
#                and no command-side journal entry on either peer, and
#                once tracking the same promote succeeds — the active's
#                field writes, receipts, and journal undisturbed
#                throughout; two passes produce identical digests
#                (refusal-failed, refusal-nondeterministic)
#                The stage's takeover leg, ci/takeover.py on the same
#                declared deployment: with the pair tracking and the
#                pump group holding a duty demand on pump 1, the
#                emitted model's declared per-pump mode seam is
#                exercised through the receipted path — p101-mode
#                cutting the delivered command off the group's cmd_1
#                with the auto-leg carriers reporting the manual
#                selection and the pump-group status reflecting the
#                exclusion, p101-hand running the pump on the operator
#                demand while the declared thermal/moisture guards
#                still gate it — the protection input driven through
#                the plant protocol asserting the proven fault and its
#                managed alarm — p101-oos asserting the maintenance
#                inhibit, and the restore returning the pump to group
#                control with the served journal carrying each
#                attributed transition in order; two passes produce
#                identical digests (takeover-failed,
#                takeover-nondeterministic)
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
#                composition is materialized pinned at the recorded
#                release rev, repinned to a later compatible revision,
#                and re-emitted — the bytes must equal the checked-in
#                model/plant.json — and the full pipeline re-runs under
#                the repin; the named incompatible crossings are refused
#                (emit-divergent, pin-unresolvable, crossing-unrefused)
#
# Environment:
#
#   DCS_REMOTE   the git remote the release crates and tooling resolve
#                from (default: the published origin below). The
#                workspace-side proof substitutes a file:// stand-in and
#                rewrites this repository's Cargo.toml to match.
#   DCS_REV      the pinned revision (default: the release-line rev
#                this repository's manifest records — the v0.2.0
#                commit whose tooling serves the interface registry,
#                declared commands, and emitted events the surface
#                stage proves).
#   DCS_UPGRADE_REV
#                the later compatible revision the upgrade stage repins
#                to (default: $DCS_REV — a same-revision repin, still
#                proving the mechanics; the workspace-side proof
#                substitutes the checkout's HEAD).
#   DCS_UPGRADE  set to 0 to skip the upgrade stage — the stage's own
#                repinned re-run uses this internally.
#   DCS_TOOLS    a directory holding prebuilt `dcs-model`,
#                `dcs-controller`, `dcs-plant-server`, and `dcs-ctl`
#                binaries. When unset, the check installs them from
#                $DCS_REMOTE at $DCS_REV — the contract's `cargo
#                install --git` mechanism — into a scratch root.

set -euo pipefail
cd "$(dirname "$0")/.."

DCS_REMOTE="${DCS_REMOTE:-https://github.com/Jan-Kaspar1/dcs.git}"
DCS_REV="${DCS_REV:-c2b5694d9fd6f6168b85c1dfc2e1542b369b3a3f}"
DCS_UPGRADE_REV="${DCS_UPGRADE_REV:-$DCS_REV}"
DCS_TOOLS="${DCS_TOOLS:-}"
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
            dcs-model dcs-controller dcs-plant dcs-monitor --root "$dir"; then
        rm -rf "$dir"
        return 1
    fi
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

echo "== resolve =="
cargo fetch --locked 2>/dev/null || {
    # --locked is the fast path; a materialized copy whose Cargo.toml was
    # rewritten to a transport stand-in re-resolves once.
    cargo fetch || fail "pin-unresolvable: cargo fetch failed for the pinned release"
}

echo "== build =="
cargo build --quiet || {
    cargo build 2>&1 | sed 's/^/  /' >&2
    fail "surface-incompatible: the composition does not compile against the pinned release"
}
TARGET_DIR="$(cargo metadata --format-version 1 --no-deps \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["target_directory"])')"
BIN="$TARGET_DIR/debug/pump-station"

echo "== lockfile =="
python3 - <<'PY' || fail "path-dependency-leak: Cargo.lock records a non-git source for a release crate"
import re, sys
lock = open("Cargo.lock").read()
sections = re.findall(r'\[\[package\]\]\nname = "([^"]+)"\nversion = "[^"]+"\nsource = "([^"]+)"', lock)
release = {name: source for name, source in sections if name.startswith("dcs-")}
missing = {"dcs-build", "dcs-core", "dcs-model"} - release.keys()
if missing:
    sys.exit(f"release crates missing from Cargo.lock: {sorted(missing)}")
for name, source in sections:
    if name.startswith("dcs-") and not source.startswith("git+"):
        sys.exit(f"{name} resolved from {source}")
PY
echo "  release crates resolve from git sources only"

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
[[ "$LINT" == *"no findings"* ]] \
    || fail "tooling-rejected: dcs-model lint reports findings: $LINT"
"$TOOLS/dcs-controller" model/plant.json --check \
    || fail "tooling-rejected: dcs-controller --check refused the checked-in model"
echo "  validate, lint, and --check accept the checked-in model"

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
for artifact in block-interfaces.schema.json plant-model.schema.json; do
    git -C "$SCRATCH" show "FETCH_HEAD:docs/releases/$DCS_RELEASE/$artifact" \
        > "$RECORD/$artifact" \
        || fail "pin-unresolvable: the pinned rev serves no docs/releases/$DCS_RELEASE/$artifact"
done

# `dcs-model <subcommand>` emitted at the pinned rev must equal the
# recorded artifact byte-for-byte — the consumer's non-drift leg for
# the served-registry and plant-model schemas the contract records as
# fetchable release artifacts. A divergence reports schema-drift on
# stderr and returns 1.
schema_nondrift() {
    local emitted
    emitted="$(mktemp)"
    if ! "$TOOLS/dcs-model" "$1" > "$emitted"; then
        rm -f "$emitted"
        echo "tooling-rejected: dcs-model $1 failed at the pinned rev" >&2
        return 1
    fi
    if ! cmp -s "$emitted" "$2"; then
        rm -f "$emitted"
        echo "schema-drift: dcs-model $1 at the pinned rev does not emit the recorded $DCS_RELEASE artifact $3" >&2
        return 1
    fi
    rm -f "$emitted"
}
schema_nondrift interface-schema "$RECORD/block-interfaces.schema.json" block-interfaces.schema.json || exit 1
schema_nondrift schema "$RECORD/plant-model.schema.json" plant-model.schema.json || exit 1
echo "  schema and interface-schema emit the $DCS_RELEASE record's artifacts byte-identically"

# A drifted artifact must report the diagnostic — the same leg against
# a doctored copy, so the recorded file stays pristine.
DOCTORED_SCHEMA="$SCRATCH/block-interfaces.doctored.json"
python3 - "$RECORD/block-interfaces.schema.json" "$DOCTORED_SCHEMA" <<'PY'
import json, sys
document = json.load(open(sys.argv[1]))
document["required"].remove("tick")
json.dump(document, open(sys.argv[2], "w"), indent=2)
PY
if out="$(schema_nondrift interface-schema "$DOCTORED_SCHEMA" block-interfaces.schema.json 2>&1)"; then
    fail "schema-drift-unchecked: a drifted record artifact passed the interface-schema non-drift leg"
fi
[[ "$out" == *"schema-drift"* ]] \
    || fail "schema-drift-unchecked: a drifted record artifact did not report schema-drift: $out"
echo "  a drifted record artifact refused: schema-drift"

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

echo "== fingerprint =="
EMITTED_FP="$("$BIN" --fingerprint)"
MANIFEST_FP="$(python3 -c 'import json; print(json.load(open("deploy/manifest.json"))["model"]["fingerprint"])')"
[ "$EMITTED_FP" = "$MANIFEST_FP" ] \
    || fail "manifest-fingerprint-mismatch: emitted model fingerprints $EMITTED_FP but deploy/manifest.json records $MANIFEST_FP"
echo "  fingerprint $EMITTED_FP matches the manifest"

echo "== deploy =="
# The rig-definition consistency check reports its own named
# diagnostics (rig-invalid, rig-unverifiable, rig-mismatch) on stderr.
python3 ci/deploy_rig.py

# The persistence fields' divergence cases, exercised against doctored
# scratch copies so the checked-in pair stays pristine: each must
# report rig-mismatch — a declared path missing its mount or flag, a
# flag or writable mount the manifest does not declare, a persistence
# mount left read-only — while the fields omitted outright (with their
# mounts and flags) stay a valid deployment.
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
elif case == "persistence-mount-read-only":
    compose = compose.replace(
        "ctrl-a-data:/var/tmp", "ctrl-a-data:/var/tmp:ro", 1)
elif case == "undeclared-persistence-flag":
    # ctrl-a keeps its --journal-file while the manifest drops the
    # field — an undeclared flag.
    document = json.loads(manifest)
    del document["controllers"][0]["journal_file"]
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
elif case == "persistence-omitted":
    # Both fields omitted together with their flags and mounts — the
    # optional deployment a consumer without durable storage declares.
    document = json.loads(manifest)
    for controller in document["controllers"]:
        controller.pop("state_file", None)
        controller.pop("journal_file", None)
    manifest = json.dumps(document, indent=2)
    for line in (
        "      - ctrl-a-data:/var/tmp\n",
        "      - ctrl-b-data:/var/tmp\n",
        "      - --state-file\n      - /var/tmp/state.json\n",
        "      - --journal-file\n      - /var/tmp/journal.jsonl\n",
    ):
        compose = compose.replace(line, "")
else:
    sys.exit("unknown rig case " + case)
open(compose_path, "w").write(compose)
open(manifest_path, "w").write(manifest)
PY
    local out
    if out="$(cd "$RIG_DIR" && python3 ci/deploy_rig.py 2>&1)"; then
        [ "$1" = "persistence-omitted" ] \
            || fail "rig-mismatch-unchecked: the $1 divergence passed the rig check"
        echo "  $1: declared fields optional — the pair still agrees"
        return
    fi
    [ "$1" = "persistence-omitted" ] \
        && fail "rig-mismatch-unchecked: omitting the persistence fields reported: $out"
    [[ "$out" == *"rig-mismatch"* ]] \
        || fail "rig-mismatch-unchecked: the $1 divergence did not report rig-mismatch: $out"
    echo "  $1 refused: rig-mismatch"
}

for divergence in persistence-mount-divergence persistence-flag-divergence \
        persistence-mount-read-only undeclared-persistence-flag \
        undeclared-writable-mount persistence-omitted; do
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
    || fail "surface-mismatch: the served operator surface — signal index, page, descriptors, interface registry, declared commands, emitted events — does not match the emitted model's declared surface"

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
# wiring statically; this leg runs it. ci/pair.py reads the standby
# target and the persistence fields out of deploy/manifest.json, spawns
# dcs-plant-server plus the two declared controllers as released
# --driven --remote instances — the standby wired --standby at its
# named peer, each controller's declared --state-file/--journal-file
# at runner-owned scratch paths — converges the standby to tracking,
# drives scans through POST /scan on each peer, issues the receipted
# demote/promote switch, and asserts the run continues bumplessly with
# the adopted receipt log identical and each peer's durable journal
# file carrying the transition records. Two passes must produce
# identical digests.
run_pair() {
    python3 ci/pair.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_pair)" \
    || fail "pair-failed: the redundant-pair leg did not hold — its evidence lines are above"
SECOND="$(run_pair)" \
    || fail "pair-failed: the redundant-pair leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "pair-nondeterministic: two pair-leg passes produced different digests"
echo "  $FIRST"

# The doctored case: a standby wired at a peer that never serves must
# surface the named diagnostic — never a silently unconverged pass.
if out="$(run_pair --tamper broken-peer-flag 2>&1)"; then
    fail "pair-unchecked: a broken peer flag passed the pair leg"
fi
[[ "$out" == *"never reported tracking"* ]] \
    || fail "pair-unchecked: the broken-peer-flag case did not report its named diagnostic: $out"
echo "  broken-peer-flag: reported, pair-failed"

# The pair contract's refusal half, on the same manifest-declared
# deployment: ci/refusal.py catches the freshly launched standby before
# its first transfer — the documented induction — where POST /promote
# must answer the named not_converged refusal with no field hand-off;
# submits a receipted write against a declared writable point to the
# tracking standby's monitor, which must answer the named not_active
# rejection with the point unchanged in the active's served snapshot,
# the write absent from both peers' adopted receipt logs, and no
# command-side journal entry on either peer recording it as anything
# but the refusal; and promotes once tracking, where the same request
# succeeds — the active's field writes, receipts, and journal
# undisturbed throughout. Two passes must produce identical digests.
run_refusal() {
    python3 ci/refusal.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_refusal)" \
    || fail "refusal-failed: the role-gated refusal leg did not hold — its evidence lines are above"
SECOND="$(run_refusal)" \
    || fail "refusal-failed: the role-gated refusal leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "refusal-nondeterministic: two refusal-leg passes produced different digests"
echo "  $FIRST"

# The doctored case: a leg asserting the standby-directed write settles
# applied must surface the named diagnostic — never a silently
# unrefused pass.
if out="$(run_refusal --tamper expect-applied 2>&1)"; then
    fail "refusal-unchecked: a doctored write expectation passed the refusal leg"
fi
[[ "$out" == *"expected an applied receipt"* ]] \
    || fail "refusal-unchecked: the expect-applied case did not report its named diagnostic: $out"
echo "  expect-applied: reported, refusal-failed"

# The pair contract's manual-takeover leg, on the same
# manifest-declared deployment: ci/takeover.py converges the pair and
# drives the simulated well until the pump group holds a duty demand on
# pump 1, then exercises the emitted model's declared per-pump mode
# seam (WW-ENG-003, WW-OPS-001, WW-CTL-002) through the receipted path
# — p101-mode cutting the delivered command off the group's cmd_1 with
# the auto-leg carriers reporting the manual selection and the
# pump-group status handing the standing demand to pump 2; p101-hand
# running the pump on the operator demand while the declared
# thermal/moisture guards still gate it, the protection input driven
# through the plant protocol asserting the proven fault and its
# managed alarm; p101-oos asserting the maintenance inhibit — then
# restores the pump to group control, auditing the active's served
# journal for each attributed transition in order. Two passes must
# produce identical digests.
run_takeover() {
    python3 ci/takeover.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_takeover)" \
    || fail "takeover-failed: the manual-takeover leg did not hold — its evidence lines are above"
SECOND="$(run_takeover)" \
    || fail "takeover-failed: the manual-takeover leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "takeover-nondeterministic: two takeover-leg passes produced different digests"
echo "  $FIRST"

# The doctored case: a leg asserting the delivered command still
# follows the group while mode stands manual must surface the named
# diagnostic — never a silently unexercised pass.
if out="$(run_takeover --tamper follows-group 2>&1)"; then
    fail "takeover-unchecked: a doctored follows-group expectation passed the takeover leg"
fi
[[ "$out" == *"did not follow the group"* ]] \
    || fail "takeover-unchecked: the follows-group case did not report its named diagnostic: $out"
echo "  follows-group: reported, takeover-failed"

echo "== consumers =="
# The boundary lint half, alongside the lockfile stage's rule: the
# stage's driver and the README's consumer obligations name only
# released artifacts and documented endpoints — never a path into a
# platform checkout.
for file in ci/consumers.py ci/ctl.py ci/deploy_rig.py ci/pair.py \
        ci/refusal.py ci/restart.py ci/schema_conformance.py \
        ci/simulate.py ci/takeover.py README.md; do
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
# composition: materialize the tree pinned at the recorded release rev,
# repin to a later compatible revision, move the lockfile, and re-run
# the check — a same-minor repin is a drop-in upgrade, so the emitted
# bytes must not change (emit-divergent). The copy keeps the working
# tree untouched.
UPGRADE_DIR="$(mktemp -d)"
for path in Cargo.toml Cargo.lock rust-toolchain.toml README.md src model deploy ci; do
    cp -r "$path" "$UPGRADE_DIR/"
done
export CARGO_TARGET_DIR="$UPGRADE_DIR/target"

# The baseline: the composition as the recorded release rev emits it.
repin "rev = \"$DCS_REV\""
( cd "$UPGRADE_DIR" && cargo fetch ) \
    || fail "pin-unresolvable: the recorded release rev $DCS_REV did not resolve"
( cd "$UPGRADE_DIR" && cargo build --quiet ) \
    || fail "surface-incompatible: the composition does not compile against the recorded release rev"
UPGRADE_BIN="$UPGRADE_DIR/target/debug/pump-station"
"$UPGRADE_BIN" > "$UPGRADE_DIR/emit-released.json"
cmp -s "$UPGRADE_DIR/emit-released.json" model/plant.json \
    || fail "emit-divergent: the recorded release rev emits different bytes than the approved model/plant.json"

# The repin: only the pin changes — src/, deploy/, and model/ are the
# unchanged tree. The fetch re-resolves and moves the copied lockfile,
# README §7's `cargo update` step.
repin "rev = \"$DCS_UPGRADE_REV\""
( cd "$UPGRADE_DIR" && cargo fetch ) \
    || fail "pin-unresolvable: the repinned revision $DCS_UPGRADE_REV did not resolve"
( cd "$UPGRADE_DIR" && cargo build --quiet ) \
    || fail "surface-incompatible: the composition does not compile against the repinned revision"
"$UPGRADE_BIN" > "$UPGRADE_DIR/emit-upgraded.json"
cmp -s "$UPGRADE_DIR/emit-upgraded.json" model/plant.json \
    || fail "emit-divergent: the unchanged composition emitted different model bytes under $DCS_UPGRADE_REV"
echo "  byte-identical emit across the repin $DCS_REV -> $DCS_UPGRADE_REV"

# The full pipeline under the repin — this check's own stages re-run
# against the repinned materialization, with the release tooling
# resolved at the repinned revision.
ensure_tools "$DCS_UPGRADE_REV" \
    || fail "pin-unresolvable: cargo install --git $DCS_REMOTE --rev $DCS_UPGRADE_REV failed"
(
    cd "$UPGRADE_DIR"
    DCS_UPGRADE=0 DCS_REMOTE="$DCS_REMOTE" DCS_REV="$DCS_UPGRADE_REV" \
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
