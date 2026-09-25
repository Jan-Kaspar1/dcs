#!/usr/bin/env python3
"""Static consistency check for the checked-in rig definition.

`deploy/compose.yaml` is the consumer-side instantiation of
`deploy/manifest.json` — the deployment declaration the platform's
release contract records. This script parses both and fails when the
definition and the manifest diverge on any field the manifest
declares:

- `dcs_release` — the definition's `x-dcs-release` extension;
- `images` — the plant service's image and every controller service's
  image;
- `model.path` / `dynamics.path` — the read-only mounts whose
  repository-relative sources equal the manifest's paths, feeding the
  model argument and `--dynamics` flag of each invocation;
- `model.fingerprint` — propagated into every controller invocation's
  environment as `DCS_MODEL_FINGERPRINT`, the identity checkpoint
  negotiation verifies on the wire;
- `plant.listen` — the plant service's `--listen` address, whose port
  the controllers' `--remote` targets by network name;
- `controllers` — one service per named controller, each `--listen`
  matching its declared address and publishing its monitor port, and
  the tracking standby's `--standby` flag plus startup ordering wired
  to the peer the manifest names;
- `controllers[].failover_budget` — the optional automatic-failover
  declaration on a standby entry (decision 28): a declared budget must
  ride the invocation's `--auto-promote` flag carrying exactly it, a
  budget the manifest omits means the flag is absent, and the field
  belongs to a tracking standby only — a duty entry declaring it
  diverges the same way;
- `controllers[].state_file` / `controllers[].journal_file` — the
  optional durability paths (decisions 35 and 36): each declared
  container path must be covered by a read-write mount and carried as
  the `--state-file`/`--journal-file` flag argument; a field the
  manifest omits means the flag is absent, and a writable mount or
  flag the manifest does not declare diverges the same way;
- the one-field-per-deployment bound (decision 98): the manifest's
  single `plant` section is one field whose single-writer claim
  admits exactly one field-owning run — a duty controller's
  conditional startup grant refuses a second live claimant
  (decision 89) — so at most one `controllers` entry may omit
  `standby`, and a second duty claimant — a second declared pair's
  duty member included — is a manifest that validates yet describes
  a rig that cannot run. Tracking entries stay unbounded: a duty
  may carry several standbys;
- `topology` — the optional named-pair index (decision 47's deferred
  plant-index artifact, the declaration a `?pair=` overview URL is
  generated from): each declared pair names two distinct member
  controllers, memberships stay disjoint across pairs, and the
  pair's wiring closes inside it — exactly one member tracks the
  other — while a standby edge into a declared pair belongs to its
  members. A member the manifest does not declare, a shared or
  duplicated member, or a declared pair whose wiring does not close
  diverges the same way; a manifest without the section is the
  single-pair default, unchanged.

The definition is parsed through `docker compose config --format json`
when a docker CLI is available — which also statically validates the
file — else through PyYAML over the file directly. Diagnostics follow
the release contract's vocabulary: `rig-invalid` when the definition
does not parse or declares no services, `rig-unverifiable` when no
parser is available, and `rig-mismatch` naming each diverging field.
On success the script prints one summary line and exits 0.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "compose.yaml"
MANIFEST = ROOT / "deploy" / "manifest.json"


def die(diagnostic, detail):
    print(f"{diagnostic}: {detail}", file=sys.stderr)
    sys.exit(1)


def load_definition():
    """Parse the rig definition; returns (document, normalized).

    `docker compose config --format json` both validates the file and
    returns the normalized model — long-syntax volumes and ports,
    absolute mount sources. Without a docker CLI a PyYAML parse of the
    file itself is the equivalent parser; the raw short syntax is
    normalized below.
    """
    docker = shutil.which("docker")
    if docker and subprocess.run(
        [docker, "compose", "version"], capture_output=True
    ).returncode == 0:
        proc = subprocess.run(
            [docker, "compose", "-f", str(COMPOSE), "config", "--format", "json"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            die(
                "rig-invalid",
                f"docker compose config rejected deploy/compose.yaml: {detail}",
            )
        return json.loads(proc.stdout), True
    try:
        import yaml
    except ImportError:
        die(
            "rig-unverifiable",
            "neither `docker compose` nor PyYAML is available to parse "
            "deploy/compose.yaml",
        )
    try:
        document = yaml.safe_load(COMPOSE.read_text())
    except Exception as exc:
        die("rig-invalid", f"deploy/compose.yaml does not parse: {exc}")
    return document, False


def repo_relative(source, normalized):
    """A mount source as a repository-relative path.

    The normalized model resolves sources to absolute paths; the raw
    file's sources are relative to the definition's own directory.
    """
    if not normalized:
        source = os.path.join(COMPOSE.parent, source)
    return os.path.relpath(source, ROOT).replace(os.sep, "/")


def view(service, normalized):
    """One service reduced to the fields this check compares."""
    command = service.get("command") or []
    if isinstance(command, str):
        command = shlex.split(command)
    environment = service.get("environment") or {}
    if isinstance(environment, list):
        environment = dict(item.split("=", 1) for item in environment)
    mounts = []
    for volume in service.get("volumes") or []:
        if isinstance(volume, str):
            parts = volume.split(":")
            # A bare name is a named volume; anything path-like is a
            # bind source, reduced to a repository-relative path.
            source = parts[0]
            named = bool(source) and not source.startswith(("/", ".", "~"))
            mounts.append(
                {
                    "source": source
                    if named
                    else repo_relative(source, normalized),
                    "target": parts[1] if len(parts) > 1 else "",
                    "ro": len(parts) > 2 and "ro" in parts[2].split(","),
                }
            )
        else:
            named = volume.get("type") == "volume"
            mounts.append(
                {
                    "source": volume.get("source", "")
                    if named
                    else repo_relative(volume.get("source", ""), normalized),
                    "target": volume.get("target", ""),
                    "ro": bool(volume.get("read_only")),
                }
            )
    published = []
    for port in service.get("ports") or []:
        try:
            if isinstance(port, str):
                parts = port.split(":")
                if len(parts) == 1:
                    published.append((None, int(parts[0])))
                else:
                    published.append((int(parts[-2]), int(parts[-1])))
            elif isinstance(port, int):
                published.append((None, port))
            else:
                target = port.get("target")
                offered = port.get("published")
                published.append((int(offered) if offered else None, int(target)))
        except (TypeError, ValueError):
            published.append(("unparseable", port))
    depends = service.get("depends_on") or {}
    if isinstance(depends, list):
        depends = {name: {} for name in depends}
    return {
        "image": service.get("image"),
        "argv": [str(arg) for arg in command],
        "env": {str(k): ("" if v is None else str(v)) for k, v in environment.items()},
        "mounts": mounts,
        "published": published,
        "depends": set(depends),
    }


def flag(argv, name):
    """The value following `name` in argv, or None when absent."""
    try:
        return argv[argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


def port_of(address):
    """The port part of a `host:port` listen address, or None."""
    try:
        return int(address.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        return None


def path_within(path, directory):
    """`path` names `directory` itself or lives beneath it."""
    return path == directory or path.startswith(directory.rstrip("/") + "/")


# The manifest's optional per-controller durability fields and the
# invocation flags that carry them (decisions 35 and 36).
PERSISTENCE = (
    ("state_file", "--state-file"),
    ("journal_file", "--journal-file"),
)


def main():
    manifest = json.loads(MANIFEST.read_text())
    document, normalized = load_definition()
    if not isinstance(document, dict):
        die("rig-invalid", "deploy/compose.yaml is not a mapping")
    services = document.get("services")
    if not isinstance(services, dict) or not services:
        die("rig-invalid", "deploy/compose.yaml declares no services")
    services = {name: view(service, normalized) for name, service in services.items()}

    mismatches = []

    def expect(condition, detail):
        if not condition:
            mismatches.append(detail)

    expect(
        document.get("x-dcs-release") == manifest["dcs_release"],
        f"x-dcs-release is {document.get('x-dcs-release')!r}, "
        f"manifest dcs_release is {manifest['dcs_release']!r}",
    )

    # The plant service is the one running the declared plant-server
    # image; its name supplies the controllers' --remote target.
    plants = [
        name
        for name, svc in services.items()
        if svc["image"] == manifest["images"]["plant_server"]
    ]
    expect(
        len(plants) == 1,
        f"expected exactly one service on image "
        f"{manifest['images']['plant_server']}, found {plants}",
    )
    plant_name = plants[0] if plants else None
    plant = services[plant_name] if plant_name else None

    if plant:
        model_mount = next(
            (m for m in plant["mounts"] if m["source"] == manifest["model"]["path"]),
            None,
        )
        expect(
            model_mount
            and model_mount["ro"]
            and model_mount["target"] == (plant["argv"] or [None])[0],
            f"the plant service must mount {manifest['model']['path']} read-only "
            f"at its model argument; mounts are {plant['mounts']}, argv is {plant['argv']}",
        )
        dynamics_mount = next(
            (
                m
                for m in plant["mounts"]
                if m["source"] == manifest["dynamics"]["path"]
            ),
            None,
        )
        expect(
            dynamics_mount
            and dynamics_mount["ro"]
            and dynamics_mount["target"] == flag(plant["argv"], "--dynamics"),
            f"the plant service must mount {manifest['dynamics']['path']} read-only "
            f"at its --dynamics path; mounts are {plant['mounts']}, "
            f"--dynamics is {flag(plant['argv'], '--dynamics')!r}",
        )
        expect(
            flag(plant["argv"], "--listen") == manifest["plant"]["listen"],
            f"the plant service --listen is {flag(plant['argv'], '--listen')!r}, "
            f"manifest plant.listen is {manifest['plant']['listen']!r}",
        )

    plant_port = port_of(manifest["plant"]["listen"])
    expect(
        plant_port is not None,
        f"manifest plant.listen {manifest['plant']['listen']!r} carries no port",
    )
    remote = f"{plant_name}:{plant_port}"
    declared = {}
    for controller in manifest["controllers"]:
        name = controller["name"]
        declared[name] = controller
        svc = services.get(name)
        expect(svc is not None, f"no service named {name} for the manifest controller")
        if svc is None:
            continue
        expect(
            svc["image"] == manifest["images"]["controller"],
            f"{name} runs image {svc['image']!r}, "
            f"manifest images.controller is {manifest['images']['controller']!r}",
        )
        expect(
            svc["env"].get("DCS_MODEL_FINGERPRINT") == manifest["model"]["fingerprint"],
            f"{name} carries DCS_MODEL_FINGERPRINT "
            f"{svc['env'].get('DCS_MODEL_FINGERPRINT')!r}, manifest "
            f"model.fingerprint is {manifest['model']['fingerprint']!r}",
        )
        model_mount = next(
            (m for m in svc["mounts"] if m["source"] == manifest["model"]["path"]),
            None,
        )
        expect(
            model_mount
            and model_mount["ro"]
            and model_mount["target"] == (svc["argv"] or [None])[0],
            f"{name} must mount {manifest['model']['path']} read-only at its "
            f"model argument; mounts are {svc['mounts']}, argv is {svc['argv']}",
        )
        expect(
            flag(svc["argv"], "--remote") == remote,
            f"{name} --remote is {flag(svc['argv'], '--remote')!r}, "
            f"expected {remote!r} (the plant service at the manifest's "
            f"plant.listen port)",
        )
        expect(
            flag(svc["argv"], "--listen") == controller["listen"],
            f"{name} --listen is {flag(svc['argv'], '--listen')!r}, "
            f"manifest listen is {controller['listen']!r}",
        )
        port = port_of(controller["listen"])
        expect(
            port is not None,
            f"manifest listen {controller['listen']!r} for {name} carries no port",
        )
        if port is not None:
            expect(
                (port, port) in svc["published"],
                f"{name} publishes {svc['published']}, expected its monitor port "
                f"{port} published as {port}",
            )
        if "standby" in controller:
            expect(
                flag(svc["argv"], "--standby") == controller["standby"],
                f"{name} --standby is {flag(svc['argv'], '--standby')!r}, "
                f"manifest standby is {controller['standby']!r}",
            )
            peer = controller["standby"].rsplit(":", 1)[0]
            expect(
                peer in svc["depends"],
                f"{name} does not order on its standby peer {peer} starting first",
            )
        else:
            expect(
                flag(svc["argv"], "--standby") is None,
                f"{name} passes --standby {flag(svc['argv'], '--standby')!r} "
                f"but the manifest declares it a duty controller",
            )
        # Automatic failover: the optional failover_budget declaration
        # arms the tracking standby's --auto-promote flag with the
        # missed-pull budget at which it self-promotes. A declared
        # budget means the flag carries exactly it and the field
        # belongs on a standby entry only; a flag the manifest does
        # not declare diverges the same way.
        budget = controller.get("failover_budget")
        promote_flag = flag(svc["argv"], "--auto-promote")
        if "standby" in controller:
            if budget is None:
                expect(
                    promote_flag is None,
                    f"{name} passes --auto-promote {promote_flag!r} but "
                    "the manifest declares no failover_budget",
                )
            else:
                expect(
                    isinstance(budget, int)
                    and not isinstance(budget, bool)
                    and budget >= 1,
                    f"manifest failover_budget {budget!r} for {name} is "
                    "not a positive integer",
                )
                expect(
                    promote_flag == str(budget),
                    f"{name} --auto-promote is {promote_flag!r}, manifest "
                    f"failover_budget is {budget!r}",
                )
        else:
            expect(
                budget is None,
                f"{name} declares failover_budget {budget!r} — automatic "
                "failover arms a tracking standby, which this entry is not",
            )
            expect(
                promote_flag is None,
                f"{name} passes --auto-promote {promote_flag!r} but the "
                "manifest declares it a duty controller",
            )
        if plant_name:
            expect(
                plant_name in svc["depends"],
                f"{name} does not order on the {plant_name} service",
            )

        # Durability: a declared state_file/journal_file must ride a
        # read-write mount — the innermost mount covering the path is
        # the one the file lands on — and the invocation flag must
        # carry it; a field the manifest omits means the flag is
        # absent, and every writable mount must back a declared path.
        declared_paths = [
            controller[field] for field, _ in PERSISTENCE if field in controller
        ]
        for field, flag_name in PERSISTENCE:
            declared_path = controller.get(field)
            actual = flag(svc["argv"], flag_name)
            if declared_path is None:
                expect(
                    actual is None,
                    f"{name} passes {flag_name} {actual!r} but the manifest "
                    f"declares no {field}",
                )
                continue
            expect(
                actual == declared_path,
                f"{name} {flag_name} is {actual!r}, manifest {field} is "
                f"{declared_path!r}",
            )
            covering = [
                m for m in svc["mounts"] if path_within(declared_path, m["target"])
            ]
            innermost = max(covering, key=lambda m: len(m["target"]), default=None)
            expect(
                innermost is not None and not innermost["ro"],
                f"{name} {field} {declared_path!r} is not covered by a "
                f"read-write mount; mounts are {svc['mounts']}",
            )
        for m in svc["mounts"]:
            if m["ro"]:
                continue
            expect(
                any(path_within(p, m["target"]) for p in declared_paths),
                f"{name} carries writable mount {m['source']}:{m['target']} "
                f"the manifest declares no state_file or journal_file under",
            )

    expect(
        set(services) == set(declared) | ({plant_name} if plant_name else set()),
        f"the definition declares services {sorted(services)}, the manifest "
        f"the plant plus controllers {sorted(declared)}",
    )

    # The manifest's own standby wiring: the peer address must name a
    # declared controller at its declared listen port.
    for controller in manifest["controllers"]:
        if "standby" not in controller:
            continue
        host, _, port = controller["standby"].rpartition(":")
        peer = declared.get(host)
        expect(
            peer is not None
            and port.isdigit()
            and port_of(peer["listen"]) == int(port),
            f"manifest standby {controller['standby']!r} does not name a "
            f"declared controller at its declared listen port",
        )

    # The one-field-per-deployment bound (decision 98): every
    # declared controller attaches to the manifest's single `plant`,
    # so each duty entry — a controller without `standby` — is a
    # claimant on that one field's single-writer claim, whose
    # conditional startup grant refuses a second live claimant
    # (decision 89). A second duty — a second declared pair's duty
    # member or a standalone entry — validates the schema yet
    # describes a rig that cannot run. Tracking entries stay
    # unbounded: a duty may carry several standbys.
    duties = sorted(
        name
        for name, controller in declared.items()
        if "standby" not in controller
    )
    expect(
        len(duties) <= 1,
        f"manifest declares {len(duties)} duty controllers {duties} "
        f"over the one plant — the field's single-writer claim "
        f"admits exactly one field-owning run (decision 98)",
    )

    # The optional topology section: the deployment's declared
    # named-pair index — the artifact a `?pair=` overview URL is
    # generated from (decision 47's deferred plant index), additive
    # over the single-pair default. Each named pair lists two
    # distinct declared controllers, memberships stay disjoint
    # across pairs, and the pair's wiring closes inside it: exactly
    # one member tracks the other. A standby edge into a declared
    # pair from a controller outside it diverges the same way.
    pair_of = {}
    topology = manifest.get("topology")
    if topology is not None:
        pairs = topology.get("pairs") if isinstance(topology, dict) else None
        expect(
            isinstance(pairs, list) and bool(pairs),
            f"manifest topology {topology!r} must declare a nonempty "
            f"pairs list",
        )
        if isinstance(pairs, list):
            named = set()
            for pair in pairs:
                name = pair.get("name") if isinstance(pair, dict) else None
                members = (
                    pair.get("members") if isinstance(pair, dict) else None
                )
                label = name if isinstance(name, str) and name else repr(pair)
                well_formed = (
                    isinstance(name, str)
                    and bool(name)
                    and isinstance(members, list)
                    and len(members) == 2
                    and all(isinstance(member, str) for member in members)
                    and members[0] != members[1]
                )
                expect(
                    well_formed,
                    f"topology pair {label} must declare a name and two "
                    f"distinct member controllers",
                )
                if not well_formed:
                    continue
                expect(
                    name not in named,
                    f"topology pair name {name!r} is declared twice",
                )
                named.add(name)
                for member in members:
                    expect(
                        member in declared,
                        f"topology pair {name!r} member {member!r} "
                        f"names no declared controller",
                    )
                    expect(
                        member not in pair_of,
                        f"{member} belongs to topology pairs "
                        f"{pair_of.get(member)!r} and {name!r}",
                    )
                    pair_of[member] = name
                trackers = [
                    member
                    for member in members
                    if member in declared and "standby" in declared[member]
                ]
                expect(
                    len(trackers) == 1,
                    f"topology pair {name!r} carries {len(trackers)} "
                    f"standby declarations — a pair is one duty "
                    f"controller tracked by one standby",
                )
                if len(trackers) == 1:
                    tracker = trackers[0]
                    peer = declared[tracker]["standby"].rsplit(":", 1)[0]
                    other = (
                        members[1] if members[0] == tracker else members[0]
                    )
                    expect(
                        peer == other,
                        f"topology pair {name!r} member {tracker} tracks "
                        f"{peer!r} outside the pair — a pair's wiring "
                        f"closes inside it",
                    )

    # A standby declaration aimed at a declared pair belongs to that
    # pair's members — a tracker outside the topology the deployment
    # declares.
    for controller in manifest["controllers"]:
        standby = controller.get("standby")
        if standby is None:
            continue
        owner = pair_of.get(standby.rsplit(":", 1)[0])
        if owner is not None:
            expect(
                pair_of.get(controller["name"]) == owner,
                f"{controller['name']} tracks {standby!r} inside "
                f"topology pair {owner!r} it does not belong to",
            )

    if mismatches:
        die(
            "rig-mismatch",
            "deploy/compose.yaml diverges from deploy/manifest.json:\n  - "
            + "\n  - ".join(mismatches),
        )
    print(
        f"deploy/compose.yaml instantiates deploy/manifest.json: "
        f"{len(declared)} controllers plus {plant_name}, "
        f"release {manifest['dcs_release']}, "
        f"fingerprint {manifest['model']['fingerprint']}"
    )


if __name__ == "__main__":
    main()
