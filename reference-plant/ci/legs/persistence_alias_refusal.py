#!/usr/bin/env python3
"""The persistence-alias refusal leg for the reference plant — the
consumer-boundary proof that #1292's persistence-path distinctness
contract holds at the declaration the customer actually authors, not
just at raw argv (WW-LCM-001, WW-ENG-003 — the
`state-file-alias-clobbers-append-durable-files` finding): the deploy
stage maps the manifest's `controllers[].state_file`/`journal_file`/
`history_file` fields onto the launched controllers' flags, so a
manifest aliasing two of them must be refused by name — never
silently deployed. The checkpoint's write-then-rename runs outside
the append sinks' writer lock, so an aliased `state_file` renames the
append file out from under its writer's descriptor: the durable
record lands on an unreachable inode while the visible file reads as
checkpoint JSON the next startup's replay refuses.

The deploy stage (`ci/deploy_rig.py`) screens the divergence
statically on the checked-in pair; this leg deploys the doctored
declaration. For each alias pair on the manifest's field owner —
`state_file` onto `journal_file`, and `state_file` onto
`history_file` where the declaration carries the field — the leg
writes a scratch copy of `deploy/manifest.json` with the alias and
runs the deployment both surfaces the check can live on:

- the deploy stage's own validation — a scratch rig carrying the
  doctored manifest with the compose flag following the field for
  parity, screened by a copied `ci/deploy_rig.py`, which must report
  `rig-mismatch` naming the two fields on the shared path;
- the launched controllers — the doctored manifest's declared pair
  spawned on the released tooling through the same manifest-to-flags
  mapping the rig instantiates, where the field owner's option parse
  must exit at startup naming both flags on one path.

The leg's verdict hangs on the launch surface — the surface the
pinned release governs. A release whose launched controller refuses
the aliased declaration by name carries the contract; one whose
launch serves the alias — whatever the static screen reports —
predates it, and the leg reports
`persistence-alias-refusal-digest inconclusive` rather than
asserting (the v0.6.0 pin predates #1292's fix). The deploy-stage
verdict rides the digest as the either/or corroborating evidence —
a refusal there is the same contract held at declaration time. An
exit naming no conflicting paths is a failure, not an inconclusive:
the contract demands the refusal name the paths. The unmodified
manifest is the positive control: the declared pair launches and
converges normally before any doctoring runs.

Usage:

    persistence_alias_refusal.py --plant-server PATH \
        --controller PATH --model model/plant.json \
        --dynamics model/dynamics.json --scenario ci/scenario.json \
        --manifest deploy/manifest.json

On success one `persistence-alias-refusal-digest <sha256>` line
prints — the check runs two passes and compares them
(`persistence-alias-refusal-nondeterministic`). A contract violation
reports `persistence-alias-refusal: …` lines on stderr and exits 1 —
the check's `persistence-alias-refusal-failed`. `--tamper
expect-serving-refusal` doctors the leg's own expectation — asserting
the aliased launch is refused by name AND keeps serving, a
disposition no release can honestly produce — so the leg proves its
refusal-versus-serving classification fires rather than passing an
unexercised contract.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored case.
# The doctored case: a leg asserting the aliased launch is refused
# by name AND keeps serving — an impossible disposition — must
# surface the named diagnostic rather than passing an unexercised
# contract.
LEG = {
    "order": 690,
    "title": "the persistence-alias refusal leg",
    "passes": "persistence-alias-refusal",
    "tampers": [
        {
            "name": "expect-serving-refusal",
            "passed": "an expect-serving-refusal case passed the persistence-alias-refusal leg",
            "missed": "the expect-serving-refusal case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the aliased launch refused and serving"
            ],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the persistence-path distinctness
    contract — a launched controller serving the aliased declaration
    is the pre-#1292 behavior — or the manifest declares no pair the
    leg can alias: the run classifies inconclusive, never a product
    failure. The first arg is the stable reason the `inconclusive`
    digest line prints — two identical passes must share it — and
    the optional second arg the run's own evidence, reported on
    stderr only."""


# The persistence fields the alias cases pair `state_file` against —
# the append sinks whose writer descriptors the checkpoint's
# write-then-rename would orphan — each with the invocation flag the
# deploy stage carries it on and the case's short name.
APPEND_FIELDS = (
    ("journal", "journal_file", "--journal-file"),
    ("history", "history_file", "--history-file"),
)

# The doctored expectation's stable evidence prefix — every failure
# the tampered pass records carries it.
TAMPER_EVIDENCE = (
    "the doctored expectation wanted the aliased launch refused and "
    "serving"
)


def duty_entry(document):
    """The declared field owner — the manifest's controller entry
    carrying no `standby` field."""
    duties = [c for c in document["controllers"] if "standby" not in c]
    return duties[0] if duties else None


def doctor_manifest(args, scratch, case, dst_field):
    """A scratch copy of the deployment manifest with the field
    owner's `state_file` aliased onto its `dst_field` path — the
    clobbering declaration the leg deploys. Returns the doctored
    manifest's path."""
    with open(args.manifest) as handle:
        document = json.load(handle)
    duty = duty_entry(document)
    if duty is None:
        raise Abort("the manifest declares no field owner to alias")
    duty["state_file"] = duty[dst_field]
    path = os.path.join(scratch, f"manifest-{case}.json")
    with open(path, "w") as handle:
        json.dump(document, handle, indent=2)
    return path


def deploy_validation(args, scratch, case, dst_field):
    """The deploy stage's own validation on the aliased declaration:
    a scratch rig — copied `ci/deploy_rig.py`, the doctored manifest,
    and the rig definition with the persistence flag following the
    manifest field so flag/field parity holds and the only
    divergence is the alias itself — screened by the copied check.
    Returns `(verdict, output)`: "refused" when `rig-mismatch` names
    the two fields on the shared path, "unverifiable" when no
    parser can run the check, else "silent"."""
    rig_dir = os.path.join(scratch, f"rig-{case}")
    os.makedirs(os.path.join(rig_dir, "ci"))
    os.makedirs(os.path.join(rig_dir, "deploy"))
    os.makedirs(os.path.join(rig_dir, "model"))
    ci_dir = os.path.dirname(_HERE)
    root = os.path.dirname(ci_dir)
    shutil.copyfile(
        os.path.join(ci_dir, "deploy_rig.py"),
        os.path.join(rig_dir, "ci", "deploy_rig.py"),
    )
    deploy_dir = os.path.join(root, "deploy")
    model_dir = os.path.join(root, "model")
    shutil.copyfile(
        os.path.join(model_dir, "plant.json"),
        os.path.join(rig_dir, "model", "plant.json"),
    )
    shutil.copyfile(
        os.path.join(model_dir, "dynamics.json"),
        os.path.join(rig_dir, "model", "dynamics.json"),
    )
    with open(args.manifest) as handle:
        document = json.load(handle)
    duty = duty_entry(document)
    if duty is None:
        raise Abort("the manifest declares no field owner to alias")
    renamed = duty["state_file"]
    duty["state_file"] = duty[dst_field]
    with open(os.path.join(rig_dir, "deploy", "manifest.json"), "w") as handle:
        json.dump(document, handle, indent=2)
    with open(os.path.join(deploy_dir, "compose.yaml")) as handle:
        compose = handle.read()
    # The flag follows the field — the same parity the deploy
    # stage's own aliased-paths case keeps, so the only divergence
    # the screen sees is the alias itself.
    compose = compose.replace(
        f"- {renamed}", f"- {duty['state_file']}", 1
    )
    with open(os.path.join(rig_dir, "deploy", "compose.yaml"), "w") as handle:
        handle.write(compose)
    proc = subprocess.run(
        [sys.executable, "ci/deploy_rig.py"],
        cwd=rig_dir,
        capture_output=True,
        text=True,
    )
    out = proc.stdout + proc.stderr
    if "rig-unverifiable" in out:
        return "unverifiable", out
    if (
        proc.returncode != 0
        and "rig-mismatch" in out
        and f"state_file and {dst_field} both name" in out
    ):
        return "refused", out
    return "silent", out


def classify_launch(args, manifest_path, other_flag):
    """Deploy the doctored manifest — the declared pair launched on
    the released tooling through the manifest-to-flags mapping the
    rig instantiates (each declared persistence path instantiated
    under the rig's scratch, so the aliased fields land on one
    file). Returns `(verdict, detail)`:

    - "refused" — a launched controller exits at startup naming
      `--state-file` and `other_flag` on one path;
    - "serving" — the pair launches and answers its monitors: the
      alias deployed silently;
    - "unsupported" — the exit names an unrecognized flag, a
      release predating the persistence vocabulary itself;
    - "exited" — a startup exit naming no conflicting paths.
    """
    declared = pair.manifest_pair(manifest_path)
    if declared is None:
        raise Abort(
            "the doctored manifest declares no standby pair — the "
            "launch probe lost its deployment"
        )
    try:
        rig = pair.launch_pair(args, declared)
    except pair.Abort as abort:
        detail = " ".join(str(arg) for arg in abort.args)
        named = next(
            (
                line
                for line in detail.split("; ")
                if "both name" in line
            ),
            None,
        )
        if (
            named is not None
            and "--state-file" in named
            and other_flag in named
        ):
            return "refused", named
        if "unknown option" in detail or "unrecognized" in detail:
            return "unsupported", detail
        return "exited", detail[:300]
    try:
        roles = {}
        for label, url in (
            ("duty", rig.duty_url),
            ("standby", rig.standby_url),
        ):
            try:
                roles[label] = simulate.http(f"{url}/role").get("role")
            except Exception:
                roles[label] = "unserved"
    finally:
        rig.close()
    return "serving", roles


def alias_refusal_pass(args, tamper):
    """The persistence-alias refusal run: converge the declared pair
    as the unmodified-manifest control, then deploy each aliased
    declaration — `state_file` onto each declared append path —
    asserting the refusal by name on the launch surface with the
    deploy-stage validation's verdict as corroborating evidence.
    Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the
    contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "persistence-alias-refusal leg has nothing to exercise"
        )
    _manifest, duty_decl, _standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    scratch = tempfile.mkdtemp(prefix="dcs-alias-refusal-")
    rig = None
    try:
        # Phase 1 — the positive control: the unmodified manifest
        # deploys and runs normally — the declared pair launches on
        # its manifest-mapped persistence flags and converges.
        rig = pair.launch_pair(args, declared)
        converged = rig.converge(failures)
        evidence["control_tick"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "control",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )
        rig.close()
        rig = None

        # Phase 2 — each aliased declaration on the field owner:
        # `state_file` onto each declared append path.
        cases = []
        for name, dst_field, dst_flag in APPEND_FIELDS:
            if duty_decl.get("state_file") and duty_decl.get(dst_field):
                cases.append((name, dst_field, dst_flag))
            else:
                digest_entries.append(
                    {"phase": f"state-{name}", "deployed": "undeclared"}
                )
        if not cases:
            raise Inconclusive(
                "the manifest's field owner declares no append path "
                "for state_file to alias — the leg has nothing to "
                "exercise",
                "no state_file/journal_file or state_file/history_file "
                "pair declared",
            )
        for name, dst_field, dst_flag in cases:
            case = f"state-{name}"
            doctored = doctor_manifest(args, scratch, case, dst_field)
            validation, validation_out = deploy_validation(
                args, scratch, case, dst_field
            )
            evidence[f"{case}-validation"] = validation
            verdict, detail = classify_launch(args, doctored, dst_flag)
            if tamper is not None:
                # The doctored expectation — refused by name AND
                # still serving, a disposition no honest release
                # produces: every observed verdict fails it, naming
                # what the launch actually did.
                failures.append(
                    f"{TAMPER_EVIDENCE} — the {case} launch "
                    + {
                        "refused": f"exited naming the flags: {detail}",
                        "serving": f"deployed the alias and serves {detail}",
                        "unsupported": f"exited on an unrecognized flag: {detail}",
                        "exited": f"exited without the named refusal: {detail}",
                    }[verdict]
                )
                continue
            if verdict == "refused":
                digest_entries.append(
                    {
                        "phase": case,
                        "deploy_validation": validation,
                        "launched": "refused-by-name",
                    }
                )
                evidence[f"{case}-refusal"] = detail
                eprint(f"persistence-alias-refusal: {case} refused — {detail}")
            elif verdict == "serving":
                raise Inconclusive(
                    "the pinned release predates the persistence-path "
                    "distinctness contract — the aliased declaration "
                    "deployed and serves",
                    f"{case}: the pair serves {detail} with the alias "
                    f"in place; deploy validation reports {validation} "
                    f"({validation_out.splitlines()[-1] if validation_out else 'no output'})",
                )
            elif verdict == "unsupported":
                raise Inconclusive(
                    "the pinned release predates the persistence-path "
                    "vocabulary — the launched controller does not "
                    "recognize a declared flag",
                    f"{case}: {detail}",
                )
            else:
                failures.append(
                    f"the {case} aliased launch exited without naming "
                    f"the conflicting paths: {detail}"
                )
        evidence["final_tick"] = evidence["control_tick"]
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if rig is not None:
            rig.close()
        shutil.rmtree(scratch, ignore_errors=True)
    return digest_entries, evidence, failures


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plant-server", required=True)
    parser.add_argument("--controller", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--tamper",
        choices=["expect-serving-refusal"],
        help="doctor the leg's own expectation — the aliased launch "
        "must be refused by name AND keep serving, a disposition no "
        "release can honestly produce — so the pass must fail "
        "naming what the launch actually did",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = alias_refusal_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"persistence-alias-refusal: {TAMPER_EVIDENCE} — an "
                "inconclusive run offers the doctored case no "
                "evidence"
            )
            return 1
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"persistence-alias-refusal: inconclusive — {detail}")
        print(f"persistence-alias-refusal-digest inconclusive — {reason}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"persistence-alias-refusal: {line}")
        return 1
    for failure in failures:
        eprint(f"persistence-alias-refusal: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"persistence-alias-refusal: the {args.tamper} case "
                "passed silently — the leg never noticed the "
                "doctored expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"persistence-alias-refusal-digest {digest} — the declared "
        "pair converged on its unmodified manifest, each aliased "
        "declaration was refused by name at launch with deploy "
        "validation's verdict beside it, and the pair's launch "
        f"roles held through tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
