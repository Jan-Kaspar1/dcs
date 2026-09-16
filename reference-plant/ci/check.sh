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
#                `dcs-controller --check` (tooling-rejected)
#   fingerprint  the emitted model's fingerprint equals the manifest's
#                recorded `model.fingerprint`
#                (manifest-fingerprint-mismatch)
#   simulate     the scripted simulation's declared outcomes hold, and
#                two runs produce identical digests (scenario-failed,
#                scenario-nondeterministic)
#
# Environment:
#
#   DCS_REMOTE   the git remote the release crates and tooling resolve
#                from (default: the published origin below). The
#                workspace-side proof substitutes a file:// stand-in and
#                rewrites this repository's Cargo.toml to match.
#   DCS_REV      the pinned revision (default: the v0.1.0 rev this
#                repository's manifest records).
#   DCS_TOOLS    a directory holding prebuilt `dcs-model`,
#                `dcs-controller`, and `dcs-plant-server` binaries. When
#                unset, the check installs them from $DCS_REMOTE at
#                $DCS_REV — the contract's `cargo install --git`
#                mechanism — into a scratch root.

set -euo pipefail
cd "$(dirname "$0")/.."

DCS_REMOTE="${DCS_REMOTE:-https://github.com/Jan-Kaspar1/dcs.git}"
DCS_REV="${DCS_REV:-a2b1b13e7b4273b133bc0fff55cb97e16c5d3197}"
TOOLS="${DCS_TOOLS:-}"
INSTALL_ROOT=""

fail() {
    echo "$1" >&2
    exit 1
}

cleanup() {
    if [ -n "$INSTALL_ROOT" ]; then rm -rf "$INSTALL_ROOT"; fi
}
trap cleanup EXIT

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
if [ -z "$TOOLS" ]; then
    INSTALL_ROOT="$(mktemp -d)"
    cargo install --quiet --git "$DCS_REMOTE" --rev "$DCS_REV" \
        dcs-model dcs-controller dcs-plant --root "$INSTALL_ROOT" \
        || fail "pin-unresolvable: cargo install --git $DCS_REMOTE --rev $DCS_REV failed"
    TOOLS="$INSTALL_ROOT/bin"
fi
"$TOOLS/dcs-model" validate model/plant.json \
    || fail "tooling-rejected: dcs-model validate refused the checked-in model"
LINT="$("$TOOLS/dcs-model" lint model/plant.json)" \
    || fail "tooling-rejected: dcs-model lint refused the checked-in model"
echo "$LINT" | grep -q "no findings" \
    || fail "tooling-rejected: dcs-model lint reports findings: $LINT"
"$TOOLS/dcs-controller" model/plant.json --check \
    || fail "tooling-rejected: dcs-controller --check refused the checked-in model"
echo "  validate, lint, and --check accept the checked-in model"

echo "== fingerprint =="
EMITTED_FP="$("$BIN" --fingerprint)"
MANIFEST_FP="$(python3 -c 'import json; print(json.load(open("deploy/manifest.json"))["model"]["fingerprint"])')"
[ "$EMITTED_FP" = "$MANIFEST_FP" ] \
    || fail "manifest-fingerprint-mismatch: emitted model fingerprints $EMITTED_FP but deploy/manifest.json records $MANIFEST_FP"
echo "  fingerprint $EMITTED_FP matches the manifest"

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
echo "check ok"
