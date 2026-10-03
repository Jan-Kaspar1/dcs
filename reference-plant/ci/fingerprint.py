#!/usr/bin/env python3
"""The manifest-fingerprint authorization leg for the reference plant
— the consumer-side proof that `deploy/manifest.json`'s recorded
`model.fingerprint` names the model bytes the deployed pair actually
serves, not just the checked-in artifact (WW-ENG-003).

The stage's static half compares `pump-station --fingerprint` — the
emitted model's digest — against the manifest's recorded value. This
leg authorizes the *served* bytes: it launches the manifest-declared
deployment on the released tooling — `dcs-plant-server` serving the
model and dynamics plus the two declared `dcs-controller --driven
--remote` peers wired per the manifest — and pulls `GET /checkpoint`
on each peer. The `model_fingerprint` a peer's checkpoint stamps is
the digest of the model document that peer was assembled from and
serves, so holding it equal to the recorded fingerprint proves the
deployment runs the fingerprinted artifact — a pair serving anything
else fails by name.

Any divergence — including a silent one the component set cannot see,
like renumbered point ids over identical components — reports
`manifest-fingerprint-mismatch`, the message naming the diverging
peer, the expected and served fingerprints, and the first top-level
document section the served model diverges in.

Usage:

    fingerprint.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `fingerprint-digest <sha256>` line prints — the check
runs two passes and compares them (`fingerprint-nondeterministic`).
A contract violation reports `manifest-fingerprint-mismatch: …` or
`fingerprint: …` lines on stderr and exits 1 — the check's
`fingerprint-failed`. `--tamper renumber-points` serves the declared
pair a doctored document — every declared point id renumbered with
its references carried, the component set verbatim — so the check
proves the diagnostic fires on the silent renumbering it exists to
catch.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "legs")
)

import pair


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The shift the `renumber-points` tamper applies to every declared
# point id — beyond the emitted document's id space, so the doctored
# document's numbering can never coincide with the fingerprinted
# artifact's.
POINT_SHIFT = 5000


def renumbered_documents(model_path, dynamics_path, scratch):
    """The doctored served documents for the `renumber-points` tamper:
    every declared point id shifted by `POINT_SHIFT`, its signal
    `source`, connection-endpoint, and dynamics `input`/`output`/
    `inputs` references carried — a valid deployment whose component
    set stays byte-identical to the fingerprinted artifact's while
    its point numbering is foreign. Written into the leg's scratch
    directory; the checked-in documents stay pristine. Returns the
    `(model, dynamics)` paths the pair serves."""
    with open(model_path) as handle:
        document = json.load(handle)
    remap = {
        point["id"]: point["id"] + POINT_SHIFT
        for point in document["io_points"]
    }
    for point in document["io_points"]:
        point["id"] = remap[point["id"]]
    for signal in document["signals"]:
        signal["source"] = remap.get(signal["source"], signal["source"])
    for connection in document["connections"]:
        for end in (connection["from"], connection["to"]):
            if "point" in end:
                end["point"] = remap.get(end["point"], end["point"])
    model_out = os.path.join(scratch, "renumbered-model.json")
    with open(model_out, "w") as handle:
        json.dump(document, handle, indent=2)

    with open(dynamics_path) as handle:
        dynamics = json.load(handle)
    for element in dynamics:
        for body in element.values():
            for field in ("input", "output"):
                value = body.get(field)
                if isinstance(value, int) and not isinstance(value, bool):
                    body[field] = remap.get(value, value)
            if isinstance(body.get("inputs"), list):
                body["inputs"] = [
                    remap.get(point, point) for point in body["inputs"]
                ]
    dynamics_out = os.path.join(scratch, "renumbered-dynamics.json")
    with open(dynamics_out, "w") as handle:
        json.dump(dynamics, handle, indent=2)
    return model_out, dynamics_out


def first_diverging_section(expected, served):
    """The first top-level section the served document diverges in,
    walked in the fingerprinted artifact's document order — the
    `io_points` section for a silent point-id renumbering. `None`
    when the documents parse identically, a state only a fingerprint
    collision could reach."""
    for key in expected:
        if key not in served or served[key] != expected[key]:
            return key
    for key in served:
        if key not in expected:
            return key
    return None


def fingerprint_pass(args, tamper):
    """The authorization run: launch the declared pair on the served
    document and hold each peer's stamped model digest equal to the
    manifest's recorded fingerprint. Returns `(digest_entries,
    evidence, mismatches, failures)` — a diverging served digest lands
    in `mismatches`, the run's own failures in `failures`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the fingerprint "
            "leg has nothing to exercise"
        )
    manifest, duty_decl, standby_decl = declared
    expected = format(int(manifest["model"]["fingerprint"], 16), "016x")
    scratch = tempfile.mkdtemp(prefix="dcs-fingerprint-")
    digest_entries, evidence, mismatches, failures = [], {}, [], []
    rig = None
    try:
        served_args = argparse.Namespace(**vars(args))
        if tamper == "renumber-points":
            served_args.model, served_args.dynamics = renumbered_documents(
                args.model, args.dynamics, scratch
            )
        rig = pair.launch_pair(served_args, declared)

        expected_doc = served_doc = None
        for name, url in (
            (duty_decl["name"], rig.duty_url),
            (standby_decl["name"], rig.standby_url),
        ):
            checkpoint = pair.get(
                f"{url}/checkpoint", "GET /checkpoint", failures
            )
            fingerprint = checkpoint.get("model_fingerprint")
            if fingerprint is None:
                failures.append(
                    f"{name}'s checkpoint carries no model_fingerprint "
                    "— the peer serves no model digest to authorize"
                )
                raise Abort
            served_fp = format(fingerprint, "016x")
            digest_entries.append({"peer": name, "served": served_fp})
            if served_fp != expected:
                if expected_doc is None:
                    with open(args.model) as handle:
                        expected_doc = json.load(handle)
                    with open(served_args.model) as handle:
                        served_doc = json.load(handle)
                section = first_diverging_section(expected_doc, served_doc)
                diverging = (
                    f'first divergence in section "{section}"'
                    if section is not None
                    else "the served document parses identically to the "
                    "fingerprinted artifact"
                )
                mismatches.append(
                    f"{name} serves model fingerprint {served_fp} but "
                    f"deploy/manifest.json records {expected}; {diverging}"
                )
        digest_entries.append({"manifest": expected})
        evidence["served"] = expected
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if rig is not None:
            rig.close()
        shutil.rmtree(scratch, ignore_errors=True)
    return digest_entries, evidence, mismatches, failures


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
        choices=["renumber-points"],
        help="serve the declared pair a doctored model — every point "
        "id renumbered with its references carried, the component set "
        "verbatim — the pass must fail naming the served divergence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, mismatches, failures = fingerprint_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"fingerprint: {line}")
        return 1
    for mismatch in mismatches:
        eprint(f"manifest-fingerprint-mismatch: {mismatch}")
    for failure in failures:
        eprint(f"fingerprint: {failure}")
    if args.tamper is not None:
        if not (mismatches or failures):
            eprint(
                f"fingerprint: the {args.tamper} case passed silently — "
                "the leg never noticed the served divergence"
            )
        return 1
    if mismatches or failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"fingerprint-digest {digest} — the deployed pair serves model "
        f"fingerprint {evidence['served']}, matching deploy/manifest.json"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
