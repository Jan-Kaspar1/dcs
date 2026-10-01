#!/usr/bin/env python3
"""The dynamics-fingerprint authorization leg for the reference plant
— the consumer-side proof that `deploy/manifest.json`'s optional
`dynamics.fingerprint` names the dynamics bytes the deployed pair
serves, not just the checked-in artifact (WW-ENG-003).

The dynamics document is simulation internals the plant server alone
consumes: `dcs-plant-server --dynamics` merges it into the served
channel map at startup, and decision 46's operator-supplied-paths rule
keeps content off the wire — no protocol surface hands a running
server's dynamics back for inspection, and this leg adds none. What
the manifest authorizes is therefore the document the deployment
itself declares and the pair is launched with: the leg resolves
`dynamics.path` out of the manifest — the same declaration
`deploy/compose.yaml`'s read-only mount and `--dynamics` flag
instantiate — launches the manifest-declared deployment on the
released tooling (`dcs-plant-server` serving the declared model and
dynamics plus the two declared `dcs-controller --driven --remote`
peers wired per the manifest), and fingerprints the bytes it served.

The fingerprint is the model fingerprint's own contract applied to
the dynamics document: FNV-1a over the parsed document's canonical
reserialization — semantic identity, not text identity — reported as
the same sixteen-hex-digit shape `model.fingerprint` carries. Three
ways to diverge, each reporting `manifest-fingerprint-mismatch`: the
served bytes against the recorded fingerprint, the checked-in
artifact against the recorded fingerprint, and the served bytes
against the checked-in artifact — a silent renumbering of every
point reference over identical element content included, the message
naming the expected and served fingerprints and the first diverging
element.

Usage:

    dynamics_fingerprint.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `dynamics-fingerprint-digest <sha256>` line prints —
the check runs two passes and compares them
(`dynamics-fingerprint-nondeterministic`). A contract violation
reports `manifest-fingerprint-mismatch: …` or
`dynamics-fingerprint: …` lines on stderr and exits 1 — the check's
`dynamics-fingerprint-failed`. `--tamper renumber-points` serves the
declared pair a doctored deployment — every declared point id
renumbered with its references carried, the element set verbatim —
so the check proves the diagnostic fires on the silent renumbering
it exists to catch. `--fingerprint PATH` prints one document's
canonical fingerprint and exits — the value a manifest's
`dynamics.fingerprint` records.
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
# deployment's numbering can never coincide with the fingerprinted
# artifact's.
POINT_SHIFT = 5000


def canonical_bytes(document):
    """The canonical byte form a dynamics fingerprint covers: the
    parsed document reserialized with sorted keys and compact
    separators — the same contract `PlantModel::fingerprint` gives the
    model, so key order and whitespace cannot perturb the digest while
    any semantic difference, a renumbered point reference included,
    fingerprints differently."""
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def fnv1a(data):
    """FNV-1a over `data`: the fixed, platform-independent hash every
    fingerprint uses — `dcs_core::ModelFingerprint::of`'s own
    algorithm, so the same document fingerprints identically
    everywhere."""
    value = 0xCBF29CE484222325
    for byte in data:
        value ^= byte
        value = (value * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return value


def document_fingerprint(document):
    """The canonical fingerprint of one parsed dynamics document —
    the sixteen-hex-digit shape `model.fingerprint` carries."""
    return format(fnv1a(canonical_bytes(document)), "016x")


def dynamics_fingerprint(path):
    """The canonical fingerprint of the dynamics document at `path`."""
    with open(path) as handle:
        return document_fingerprint(json.load(handle))


def renumbered_documents(model_path, dynamics_path, scratch):
    """The doctored served documents for the `renumber-points` tamper:
    every declared point id shifted by `POINT_SHIFT`, its signal
    `source`, connection-endpoint, and dynamics `input`/`output`/
    `inputs` references carried — a valid deployment whose element set
    stays byte-identical to the fingerprinted artifact's while its
    point numbering is foreign. Written into the leg's scratch
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


def first_diverging_element(expected, served):
    """The first list position the served dynamics document diverges
    at — every element under a point-id renumbering. `None` when the
    documents parse identically, a state only a fingerprint collision
    could reach."""
    for index, element in enumerate(expected):
        if index >= len(served):
            return (
                f"element {index} (the served document ends at "
                f"{len(served)} elements)"
            )
        if served[index] != element:
            kind = next(iter(element), "unknown")
            return f'element {index} ("{kind}")'
    if len(served) > len(expected):
        return f"element {len(expected)} (the served document adds elements)"
    return None


def fingerprint_pass(args, tamper):
    """The authorization run: launch the declared pair on the served
    dynamics document and hold its fingerprint equal to the manifest's
    recorded `dynamics.fingerprint` and the checked-in artifact's.
    Returns `(digest_entries, evidence, mismatches, failures)` — a
    diverging served digest lands in `mismatches`, the run's own
    failures in `failures`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "dynamics-fingerprint leg has nothing to exercise"
        )
    manifest, duty_decl, standby_decl = declared
    declared_dynamics = manifest.get("dynamics", {}).get("path")
    if declared_dynamics is None:
        raise Abort(
            "the manifest declares no dynamics.path — the deployment "
            "serves no dynamics document to authorize"
        )
    recorded = manifest["dynamics"].get("fingerprint")
    scratch = tempfile.mkdtemp(prefix="dcs-dynamics-fingerprint-")
    digest_entries, evidence, mismatches, failures = [], {}, [], []
    rig = None
    try:
        served_args = argparse.Namespace(**vars(args))
        served_args.dynamics = declared_dynamics
        if tamper == "renumber-points":
            served_args.model, served_args.dynamics = renumbered_documents(
                args.model, declared_dynamics, scratch
            )
        rig = pair.launch_pair(served_args, declared)

        # The served census: the running plant answers `list_points`
        # over the channel map the declared model and dynamics merged
        # into — the deployment observably serving the bytes the leg
        # fingerprints.
        census = rig.plant_io.request({"op": "list_points"})
        points = census.get("points") if isinstance(census, dict) else None
        if not isinstance(points, list):
            failures.append(
                f"the plant's list_points answered {census}, expected "
                "the served point census"
            )
            raise Abort

        with open(args.dynamics) as handle:
            checked_doc = json.load(handle)
        with open(served_args.dynamics) as handle:
            served_doc = json.load(handle)
        checked_fp = document_fingerprint(checked_doc)
        served_fp = document_fingerprint(served_doc)

        digest_entries.append(
            {
                "served": served_fp,
                "checked_in": checked_fp,
                "manifest": recorded,
                "points": len(points),
            }
        )
        evidence["served"] = served_fp

        diverging = None
        if served_fp != checked_fp or (
            recorded is not None and served_fp != recorded
        ):
            diverging = first_diverging_element(checked_doc, served_doc)
            diverging = (
                f"first divergence at {diverging}"
                if diverging is not None
                else "the served document parses identically to the "
                "checked-in artifact"
            )
        if recorded is not None and served_fp != recorded:
            mismatches.append(
                f"the deployed pair serves dynamics fingerprint "
                f"{served_fp} but deploy/manifest.json records "
                f"{recorded}; {diverging}"
            )
        if checked_fp != recorded and recorded is not None:
            mismatches.append(
                f"the checked-in dynamics document fingerprints "
                f"{checked_fp} but deploy/manifest.json records "
                f"{recorded}"
            )
        if served_fp != checked_fp:
            mismatches.append(
                f"the deployed pair serves dynamics fingerprint "
                f"{served_fp} while the checked-in artifact "
                f"fingerprints {checked_fp}; {diverging}"
            )
        digest_entries.append(
            {"peers": [duty_decl["name"], standby_decl["name"]]}
        )
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
    parser.add_argument("--plant-server")
    parser.add_argument("--controller")
    parser.add_argument("--model")
    parser.add_argument("--dynamics")
    parser.add_argument("--scenario")
    parser.add_argument("--manifest")
    parser.add_argument(
        "--fingerprint",
        metavar="PATH",
        help="print PATH's canonical dynamics fingerprint and exit — "
        "the value a manifest's dynamics.fingerprint records",
    )
    parser.add_argument(
        "--tamper",
        choices=["renumber-points"],
        help="serve the declared pair a doctored deployment — every "
        "point id renumbered with its references carried, the element "
        "set verbatim — the pass must fail naming the served "
        "divergence",
    )
    args = parser.parse_args()

    if args.fingerprint is not None:
        print(dynamics_fingerprint(args.fingerprint))
        return 0

    for field in (
        "plant_server",
        "controller",
        "model",
        "dynamics",
        "scenario",
        "manifest",
    ):
        if getattr(args, field) is None:
            parser.error(f"--{field.replace('_', '-')} is required")

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, mismatches, failures = fingerprint_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"dynamics-fingerprint: {line}")
        return 1
    for mismatch in mismatches:
        eprint(f"manifest-fingerprint-mismatch: {mismatch}")
    for failure in failures:
        eprint(f"dynamics-fingerprint: {failure}")
    if args.tamper is not None:
        if not (mismatches or failures):
            eprint(
                f"dynamics-fingerprint: the {args.tamper} case passed "
                "silently — the leg never noticed the served divergence"
            )
        return 1
    if mismatches or failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"dynamics-fingerprint-digest {digest} — the deployed pair "
        f"serves dynamics fingerprint {evidence['served']}, matching "
        "deploy/manifest.json"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
