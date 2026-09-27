#!/usr/bin/env python3
"""The deployment's `?pair=` overview URL generator — the consumer
half of decision 47's recorded mechanism. `deploy/manifest.json`'s
optional `topology` section is the declared pair index the monitor
page's overview mode configures from; this tool reads the manifest
and the rig definition, resolves each declared member to the
published monitor endpoint the deployment exposes, and prints the
overview URL — the serving origin carrying one
`pair=<name>=<host:port>,<host:port>` parameter per declared pair:

    $ python3 ci/overview_url.py
    http://localhost:8080/?pair=station=localhost:8080,localhost:8081

Each member resolves to the `ports` publication the rig definition
exposes for the member's declared `listen` port, dialed through
`--host` (default `localhost` — the single-host rig's dial
address); `--member <name>=<host:port>` names a member's endpoint
outright — the multi-host shape, where each member's monitor is
dialed at its own host — and `--origin <host:port>` names the
monitor serving the page (default the first resolved member's
endpoint). Generation is pure declaration: nothing here is probed.
The pair stage's `pair-overview` leg (`ci/legs/pair_overview.py`)
is the generation's live half — it asserts the generated query
names exactly the manifest's declared pairs and that every
generated member endpoint answers `GET /role` as a serving
monitor, and pins the derivation against the fixture manifest
shapes `self_test` covers: the declared single pair, the
section-less single-pair default, and the lone controller.

A manifest without the `topology` section is the single-pair
default: the pair derives from the standby wiring instead — the
duty member first, then its trackers — emitted unnamed, the page
standing the first peer's address in as the card's name; a
lone-controller manifest derives a one-member pair. A malformed
`pairs` entry, a member naming no declared controller, or a
declared member resolving to no published endpoint fails
`overview-url: …` on stderr — the topology grammar's remaining
referential rules (disjoint memberships, the pair's wiring closing
inside it, the one-duty-per-deployment bound) stay the deploy
stage's `rig-mismatch`, which runs on the same manifest.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deploy_rig

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy" / "manifest.json"
COMPOSE = ROOT / "deploy" / "compose.yaml"


def eprint(*args):
    print(*args, file=sys.stderr)


class Invalid(Exception):
    """A declaration the query cannot derive from — malformed in a
    way the deploy stage's rig agreement would also diverge on."""


class Unserved(Exception):
    """A declared member resolving to no published monitor endpoint
    — the deployment exposes no monitor the query could name for
    it. The argument is the member's name."""


def declared_pairs(manifest):
    """The deployment's pairs in derivation order — a list of
    `{"name": <str|None>, "members": [<controller>, <controller>]}`.

    With a `topology` section: its declared pairs, each name and
    two distinct member controllers required, every member naming
    a `controllers` entry. Without: the single-pair default —
    controllers grouped by the standby anchor they track (or their
    own name when they track nothing), the anchor member first and
    the group unnamed — so a lone controller derives a one-member
    pair.
    """
    controllers = manifest.get("controllers") or []
    declared = set()
    for controller in controllers:
        name = controller.get("name") if isinstance(controller, dict) else None
        if not isinstance(name, str) or not name:
            raise Invalid("a controllers entry carries no name")
        declared.add(name)

    topology = manifest.get("topology")
    if topology is None:
        groups = {}
        order = []
        for controller in controllers:
            name = controller["name"]
            standby = controller.get("standby")
            anchor = (
                standby.rsplit(":", 1)[0]
                if isinstance(standby, str)
                else name
            )
            if anchor not in groups:
                groups[anchor] = []
                order.append(anchor)
            if name not in groups[anchor]:
                groups[anchor].append(name)
        pairs = []
        for anchor in order:
            members = groups[anchor]
            if anchor in declared and members[0] != anchor:
                members = [anchor] + [
                    member for member in members if member != anchor
                ]
            pairs.append({"name": None, "members": members})
        return pairs

    pairs = topology.get("pairs") if isinstance(topology, dict) else None
    if not isinstance(pairs, list) or not pairs:
        raise Invalid(
            f"manifest topology {topology!r} must declare a nonempty "
            "pairs list"
        )
    derived = []
    for entry in pairs:
        name = entry.get("name") if isinstance(entry, dict) else None
        members = entry.get("members") if isinstance(entry, dict) else None
        label = name if isinstance(name, str) and name else repr(entry)
        if not (
            isinstance(name, str)
            and name
            and isinstance(members, list)
            and len(members) == 2
            and all(isinstance(member, str) for member in members)
            and members[0] != members[1]
        ):
            raise Invalid(
                f"topology pair {label} must declare a name and two "
                "distinct member controllers"
            )
        for member in members:
            if member not in declared:
                raise Invalid(
                    f"topology pair {name!r} member {member!r} names "
                    "no declared controller"
                )
        derived.append({"name": name, "members": list(members)})
    return derived


def pair_spec(name, members, resolve):
    """One `pair=` parameter's value — `[name=]<host:port>,…` —
    resolving each member through `resolve` (member name to the
    `host:port` monitor endpoint the deployment exposes for it, or
    None when none is exposed)."""
    endpoints = []
    for member in members:
        endpoint = resolve(member)
        if endpoint is None:
            raise Unserved(member)
        endpoints.append(endpoint)
    spec = ",".join(endpoints)
    return f"{name}={spec}" if name else spec


def overview_query(manifest, resolve):
    """The `?pair=` query the manifest's topology derives — one
    `pair=<spec>` parameter per declared pair, in declaration
    order; empty when the manifest declares no controllers."""
    return "&".join(
        f"pair={pair_spec(pair['name'], pair['members'], resolve)}"
        for pair in declared_pairs(manifest)
    )


def overview_url(manifest, resolve, origin=None):
    """The full overview URL — `origin`'s monitor page carrying the
    generated query; the first resolved member's endpoint stands in
    when no origin is given."""
    pairs = declared_pairs(manifest)
    query = "&".join(
        f"pair={pair_spec(pair['name'], pair['members'], resolve)}"
        for pair in pairs
    )
    if origin is None:
        if not pairs or not pairs[0]["members"]:
            raise Invalid(
                "the manifest declares no controller endpoint to "
                "serve the page — pass --origin"
            )
        origin = resolve(pairs[0]["members"][0])
        if origin is None:
            raise Unserved(pairs[0]["members"][0])
    return f"http://{origin}/" + (f"?{query}" if query else "")


def published_endpoints(manifest, host, compose=None):
    """Each declared controller's published monitor endpoint —
    `{host}:{published}` where `published` is the `ports` offering
    the rig definition exposes for the controller's declared
    `listen` port. A controller with no service, no parseable
    listen port, or no matching publication is absent from the
    map — the resolver then reports it unserved. The definition is
    parsed through the same `docker compose config` / PyYAML path
    the deploy stage's rig agreement uses."""
    if compose is not None:
        deploy_rig.COMPOSE = Path(compose)
    document, normalized = deploy_rig.load_definition()
    services = document.get("services") or {}
    endpoints = {}
    for controller in manifest.get("controllers") or []:
        name = controller.get("name")
        port = deploy_rig.port_of(controller.get("listen") or "")
        service = services.get(name)
        if service is None or port is None:
            continue
        for offered, target in deploy_rig.view(service, normalized)[
            "published"
        ]:
            if target == port and isinstance(offered, int):
                endpoints[name] = f"{host}:{offered}"
                break
    return endpoints


def self_test():
    """The URL derivation pinned on the manifest shapes the grammar
    produces — the checked-in declared single pair, the section-less
    single-pair default the standby wiring implies, and the
    lone-controller deployment. Returns `[(label, query)]`; raises
    `Invalid` on a divergence from the pinned expectation."""
    cases = [
        (
            "declared-pair",
            {
                "controllers": [
                    {"name": "ctrl-a", "listen": "0.0.0.0:8080"},
                    {
                        "name": "ctrl-b",
                        "listen": "0.0.0.0:8081",
                        "standby": "ctrl-a:8080",
                    },
                ],
                "topology": {
                    "pairs": [
                        {"name": "station", "members": ["ctrl-a", "ctrl-b"]}
                    ]
                },
            },
            "pair=station=ops-a:8080,ops-b:8081",
        ),
        (
            "single-pair-default",
            {
                "controllers": [
                    {"name": "ctrl-a", "listen": "0.0.0.0:8080"},
                    {
                        "name": "ctrl-b",
                        "listen": "0.0.0.0:8081",
                        "standby": "ctrl-a:8080",
                    },
                ],
            },
            "pair=ops-a:8080,ops-b:8081",
        ),
        (
            "lone-controller",
            {
                "controllers": [
                    {"name": "ctrl-a", "listen": "0.0.0.0:8080"},
                ],
            },
            "pair=ops-a:8080",
        ),
    ]
    endpoints = {"ctrl-a": "ops-a:8080", "ctrl-b": "ops-b:8081"}
    derived = []
    for label, manifest, expected in cases:
        actual = overview_query(manifest, endpoints.get)
        if actual != expected:
            raise Invalid(
                f"the {label} fixture derived {actual!r}, expected "
                f"{expected!r}"
            )
        derived.append((label, actual))
    return derived


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--manifest",
        default=str(MANIFEST),
        help="the deployment manifest the topology derives from "
        "(default: deploy/manifest.json)",
    )
    parser.add_argument(
        "--compose",
        default=str(COMPOSE),
        help="the rig definition whose ports publications the "
        "members resolve through (default: deploy/compose.yaml)",
    )
    parser.add_argument(
        "--host",
        default="localhost",
        help="the dial host for the rig's published ports (default: "
        "localhost — the single-host rig)",
    )
    parser.add_argument(
        "--member",
        action="append",
        default=[],
        metavar="NAME=HOST:PORT",
        help="resolve member NAME to the endpoint the deployment "
        "exposes for it — the multi-host shape; repeatable",
    )
    parser.add_argument(
        "--origin",
        metavar="HOST:PORT",
        help="the monitor serving the overview page (default: the "
        "first resolved member's endpoint)",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run the fixture derivations and print each label with "
        "its generated query",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        try:
            for label, query in self_test():
                print(f"{label}: ?{query}")
        except Invalid as invalid:
            eprint(f"overview-url: {invalid}")
            return 1
        return 0

    with open(args.manifest) as handle:
        manifest = json.load(handle)

    explicit = {}
    for item in args.member:
        name, sep, endpoint = item.partition("=")
        if not sep or not name or not endpoint:
            eprint(
                f"overview-url: --member {item!r} is not NAME=HOST:PORT"
            )
            return 1
        explicit[name] = endpoint

    published = {}

    def resolve(member):
        if member in explicit:
            return explicit[member]
        if not published:
            published.update(
                published_endpoints(manifest, args.host, args.compose)
            )
        return published.get(member)

    try:
        print(overview_url(manifest, resolve, args.origin))
    except Unserved as unserved:
        eprint(
            f"overview-url: member {unserved.args[0]!r} resolves to no "
            "published monitor endpoint — the deployment exposes no "
            "monitor for it"
        )
        return 1
    except Invalid as invalid:
        eprint(f"overview-url: {invalid}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
