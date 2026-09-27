#!/usr/bin/env python3
"""The pair-overview URL leg for the reference plant — the
consumer-side proof that the deployment manifest's declared pair
index generates the monitor page's `?pair=` overview query with no
hand-transcription, and that every member endpoint the query names
answers as a serving monitor (WW-ENG-003 — decision 47's deferred
plant-index artifact, the declaration a `?pair=` overview URL is
generated from).

`deploy/manifest.json`'s optional `topology` section is that
declaration; `ci/overview_url.py` is the documented generator — it
reads the section and resolves each declared member to the
published monitor endpoint the deployment exposes, emitting one
`pair=<name>=<host:port>,<host:port>` query parameter per declared
pair. This leg is the generation's live half. The rig is the pair
legs' shared one — `pair.launch_pair` launches the
manifest-declared standby pair on the released tooling — and the
run:

- converges the declared standby to `tracking`, so the roles the
  generated endpoints then answer are the pair's settled ones;
- derives the overview query through `ci/overview_url.py`, each
  declared member resolving to the monitor endpoint the launched
  deployment serves — the runtime stand-in for the rig
  definition's `ports` publications the checked-in deployment
  exposes — and asserts the generated query names exactly the
  manifest's declared pairs, each `pair=` parameter carrying its
  pair's declared name and its members' resolved endpoints in
  declaration order;
- probes every generated member endpoint as a serving monitor —
  `GET /role` answering the peer's settled report, the field
  owner `active` and its tracker `standby`;
- pins the derivation on the manifest shapes the grammar produces
  — the declared single pair, the section-less single-pair
  default, and the lone controller — through the generator's own
  fixture cases.

Usage:

    pair_overview.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `pair-overview-digest <sha256>` line prints — the
check runs two passes and compares them
(`pair-overview-nondeterministic`). A contract violation reports
`pair-overview: …` lines on stderr and exits 1 — the check's
`pair-overview-failed`. `--tamper unserved-member` doctors the
topology the leg generates from to name a member the manifest
declares but the deployment serves no monitor endpoint for, so the
leg proves its named-member diagnostic fires rather than
generating a dead reference.
"""

import argparse
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import overview_url
import pair


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a topology naming a member the deployment
# serves no monitor endpoint for must surface the named-member
# diagnostic — never a generated dead reference passing as the
# deployment's overview URL.
LEG = {
    "order": 530,
    "title": "the pair-overview URL leg",
    "passes": "pair-overview-leg",
    "tampers": [
        {
            "name": "unserved-member",
            "passed": "a topology naming a member with no serving endpoint passed the pair-overview leg",
            "missed": "the unserved-member case did not report its named diagnostic",
            "evidence": ["ctrl-c", "no serving monitor endpoint"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


def addr(url):
    """The peer's dialable monitor address as `host:port` — the
    member form the `?pair=` convention lists."""
    return url.removeprefix("http://")


def parse_query(query):
    """The generated query parsed back — `[(name, [endpoint,…]),…]`
    in parameter order, `None` for a pair left unnamed (the page
    stands the first peer's address in as the card's name)."""
    parsed = []
    if not query:
        return parsed
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key != "pair":
            raise Abort(
                f"the generated query carries {part!r}, not a pair "
                "parameter"
            )
        name, sep, spec = value.partition("=")
        if not sep:
            name, spec = None, name
        parsed.append((name, spec.split(",")))
    return parsed


def pair_overview_pass(args, tamper):
    """The pair-overview run: launch the manifest-declared pair,
    generate the `?pair=` query from the declared topology over the
    deployment's serving endpoints, assert the query's names and
    member resolutions, and probe every generated endpoint's
    `/role` for its settled report. Returns `(digest_entries,
    evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "pair-overview leg has nothing to exercise"
        )
    manifest, duty_decl, standby_decl = declared
    expected_role = {
        duty_decl["name"]: "active",
        standby_decl["name"]: "standby",
    }
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        endpoints = {
            duty_decl["name"]: addr(rig.duty_url),
            standby_decl["name"]: addr(rig.standby_url),
        }

        # Convergence first — the roles the generated endpoints
        # then answer are the pair's settled ones.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]

        generation_manifest = manifest
        if tamper == "unserved-member":
            # The doctored topology names a controller the manifest
            # declares but the deployment launches — and serves —
            # no monitor endpoint for.
            generation_manifest = json.loads(json.dumps(manifest))
            generation_manifest["controllers"].append(
                {"name": "ctrl-c", "listen": "0.0.0.0:8082"}
            )
            generation_manifest["topology"] = {
                "pairs": [
                    {
                        "name": "station",
                        "members": [duty_decl["name"], "ctrl-c"],
                    }
                ]
            }

        try:
            pairs = overview_url.declared_pairs(generation_manifest)
            query = overview_url.overview_query(
                generation_manifest, endpoints.get
            )
        except overview_url.Unserved as unserved:
            failures.append(
                f"the declared topology names member "
                f"{unserved.args[0]!r} with no serving monitor "
                "endpoint — the deployment exposes no monitor for "
                "it, so the query cannot name it"
            )
            raise Abort
        except overview_url.Invalid as invalid:
            failures.append(
                f"the declared topology did not generate: {invalid}"
            )
            raise Abort

        # The read-back assertions: the query names exactly the
        # manifest's declared pairs, and each pair's member list
        # resolves to the members' serving monitor endpoints in
        # declaration order.
        parsed = parse_query(query)
        declared_names = [entry["name"] for entry in pairs]
        generated_names = [name for name, _endpoints in parsed]
        if generated_names != declared_names:
            failures.append(
                f"the generated query names pairs {generated_names}, "
                f"the manifest's topology declares {declared_names}"
            )
        resolved = []
        for (name, spec_endpoints), entry in zip(parsed, pairs):
            want = [endpoints[member] for member in entry["members"]]
            if spec_endpoints != want:
                failures.append(
                    f"generated pair {name!r} resolves to "
                    f"{spec_endpoints}; its declared members' serving "
                    f"endpoints are {want}"
                )
            resolved.extend(zip(entry["members"], spec_endpoints))
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "generate",
                "pairs": [
                    {"name": entry["name"], "members": entry["members"]}
                    for entry in pairs
                ],
            }
        )

        # Every generated member endpoint answers as a serving
        # monitor — the peer's settled `GET /role` report.
        roles = {}
        for member, endpoint in resolved:
            report = pair.get(
                f"http://{endpoint}/role",
                f"GET /role on {endpoint} (pair member {member})",
                failures,
            )
            role = report.get("role") if isinstance(report, dict) else None
            roles[member] = role
            want = expected_role.get(member)
            if want is not None and role != want:
                failures.append(
                    f"{endpoint} — generated pair member {member!r} — "
                    f"answers /role {report}, expected the peer's "
                    f"settled {want} report"
                )
        if failures:
            raise Abort
        digest_entries.append({"phase": "role", "roles": roles})

        # The derivation pinned on the manifest shapes the grammar
        # produces — the declared single pair, the section-less
        # single-pair default, and the lone controller.
        try:
            fixtures = overview_url.self_test()
        except overview_url.Invalid as invalid:
            failures.append(f"the derivation's fixtures diverged: {invalid}")
            raise Abort
        digest_entries.append(
            {
                "phase": "fixtures",
                "queries": [query for _label, query in fixtures],
            }
        )
        evidence["pairs"] = ", ".join(
            entry["name"] or entry["members"][0] for entry in pairs
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if rig is not None:
            rig.close()
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
        choices=["unserved-member"],
        help="doctor the topology the leg generates from — a member "
        "with no serving endpoint must fail naming it",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = pair_overview_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"pair-overview: {line}")
        return 1
    for failure in failures:
        eprint(f"pair-overview: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"pair-overview: the {args.tamper} case passed "
                "silently — the leg never noticed the unserved member"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"pair-overview-digest {digest} — the declared topology "
        f"generates pair(s) {evidence['pairs']}, every member "
        "endpoint answering /role"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
