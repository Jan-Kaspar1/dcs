#!/usr/bin/env python3
"""The foreign-claim-release leg for the reference plant — the
consumer-side proof that on the manifest-declared *unkeyed* pair a
held monitor-less foreign claim leaves the fenced ex-owner's serving
monitor reporting `unsynchronized` only for the claim's standing
window, and that releasing the claim resolves the field through the
ex-owner's recorded reclaim or a successor's declared-monitor
adoption — journaled, reconverged, never stranded (WW-ENG-003,
WW-LCM-001 — the #1167 contract the rig's field-attested-source
scenarios pin in-workspace, mirrored at the customer boundary).

The claim-reclaim leg (`ci/legs/claim_reclaim.py`) proves the
preempt-and-reclaim lifecycle on the keyed launch, and the
claim-monitor-rendezvous leg proves the declared monitor is dialable
under the manifest's wildcard binds. This leg pins the finding's own
deployment shape: the manifest declares no `--pair-token` and binds
every controller on `0.0.0.0`, so a demoted ex-owner can prove no
announced hint — the standing claim's declared monitor is the only
rendezvous the field's own arbitration supplies. While the foreign
claim declares none, `unsynchronized` is the honest report: the field
names nothing to track. The defect this leg convicts is the parked
one — `unsynchronized` surviving the claim's release.

The pair launches unkeyed on its declared wildcard binds — the rig's
`pair.launch_pair` keys the pair for the announced-source contracts,
so this leg composes the same spawn sequence without the token. With
the manifest-declared pair converged to `active`/`tracking`, the run
stages the issue's two release resolutions, the reclaim first so the
ex-owner still carries no learned tracking pin — both episodes then
exercise the strict `unsynchronized` window the finding names:

- a dedicated plant-socket attachment's monitor-less `claim_writer`
  — the unconditional foreign preempt, marked `controller: false`
  the way a field tool's claim stands — fences the owner; the
  superseded active's first fenced write demotes it in place with a
  `field_claim_lost` attributed to the foreign token;
- the standing window: across driven scans the ex-owner must report
  `standby`/`unsynchronized` — and only that — while the monitor-less
  claim stands, the sibling stays an unarmed standby, the fencing
  verdict keeps naming the foreign owner with no monitor declared,
  and the shipped `dcs-plant-ctl` answers the census while its field
  mutation meets the named fencing refusal;
- the release resolves two ways, each exercised: the reclaim path —
  the ex-owner's own bound re-grant re-seating the released field
  under its recorded token, walking `standby → promoting → active`
  under the `reclaim` origin, unattended — runs first, leaving the
  launch roles standing for the second episode; the successor path
  releases and immediately promotes the orphaned sibling — its
  conditional claim lands declaring its dialable monitor before the
  ex-owner's own re-grant probe could run — and the ex-owner
  resolves the claim-declared endpoint through the verified
  tracking path, journaling `tracking_source_adopted` and
  converging `tracking`;
- both episodes journal their loss and resolution records into the
  manifest-declared durable journal file, and the run ends with the
  documented switch restoring the launch roles and claim state.

The `claim_writer`/`release_writer` staging rides the dedicated
attachment on the same plant-socket protocol the shipped
`dcs-plant-ctl` speaks — the claim lifecycle is a driver surface the
tool's verb list deliberately does not spell — while the tool itself
drives the third-party fencing probes (`list` answering the unfenced
census, `step` refused with the named fencing detail).

The contract postdates the pinned release line: where the launched
tooling predates it — no owner-token claim line, an unanswered claim
verb, a fencing verdict naming no owner or no declared monitor, the
wildcard bind stored verbatim, a checkpoint without the ownership
stamps, a standby report carrying no sync vocabulary, a loss or
adoption unjournaled — the run's own evidence is the pre-contract
shape and the leg reports `foreign-claim-release-digest inconclusive`
rather than asserting until the manifest repins a release carrying
the contract.

Usage:

    foreign_claim_release.py --plant-server PATH --controller PATH \
        --plant-ctl PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `foreign-claim-release-digest <sha256>` line prints —
the check runs two passes and compares them
(`foreign-claim-release-nondeterministic`). A contract violation
reports `foreign-claim-release: …` lines on stderr and exits 1 — the
check's `foreign-claim-release-failed`. `--tamper phantom-release`
doctors the hand-back: the claim stands as a live controller
incumbent and the release lands on an attachment that holds nothing,
so the peer strands past the claim's release and the leg must report
the named diagnostic rather than passing an unexercised contract.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import claim_reclaim
import failover
import pair
import simulate
import stranded_rejoin


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a release that lands on an attachment holding
# nothing while the claim stands as a live controller incumbent —
# the released field never resolving — must surface the named
# diagnostic rather than passing an unexercised contract.
LEG = {
    "order": 610,
    "title": "the foreign-claim-release leg",
    "passes": "foreign-claim-release",
    "tools": {
        "plant-ctl": "dcs-plant-ctl",
    },
    "tampers": [
        {
            "name": "phantom-release",
            "passed": "a phantom-release case passed the foreign-claim-release leg",
            "missed": "the phantom-release case did not report its named diagnostic",
            "evidence": ["the released field never resolved"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates — or never covers — the contract
    the leg exercises: the run classifies inconclusive, never a
    product failure. The first arg is the stable reason the
    `inconclusive` digest line prints — two identical passes must
    share it — and the optional second arg the run's own evidence,
    reported on stderr only, where ephemeral verdicts, endpoints,
    and minted tokens belong."""


# The driven-scan bounds the episode's phases run: the fenced demote
# settles inside a couple of scans, the claim-standing window spans a
# handful of driven scans, and the release's resolution — the
# successor's declared-monitor adoption or the ex-owner's recorded
# reclaim — is the bounded window the contract names, the defect's
# indefinite `unsynchronized` being the failure the bound catches.
WATCH_SCANS = 6
HOLD_SCANS = 3
RESOLVE_SCANS = 12

# The dedicated attachment's foreign owner token — a small fixed
# value that cannot collide with a controller's per-process minted
# token, distinct from the tokens the other legs stage.
FOREIGN_CLAIM = 0xF04C

# The substring the shipped tool's stderr carries for a fenced field
# mutation — the named refusal `dcs-plant-ctl` reports under a
# standing foreign claim.
FENCED_DETAIL = "another attachment owns field writes"


def plant_ctl(tool, addr, *argv):
    """One shipped `dcs-plant-ctl` invocation against the pair's
    spawned plant — the census and the third-party mutation probes
    ride the released tool's surface. Returns `(exit, stdout,
    stderr)`."""
    run = subprocess.run(
        [tool, addr, *argv],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return run.returncode, run.stdout, run.stderr


def monitor_addr(url):
    """The peer's dialable monitor address as `host:port` — the form
    the normalized claim declaration names once the wildcard bind
    resolves to the claim connection's proven source."""
    return url.removeprefix("http://")


def monitor_kind(declared, owner_addr):
    """Classify a claim-declared monitor: `wildcard` for an
    unspecified bind address — the verbatim `0.0.0.0` declaration a
    fenced peer dials as its own loopback — `owner` for the claiming
    peer's own dialable monitor, `foreign` for anything else. Only
    `owner` is a contract answer."""
    text = str(declared)
    host = text.rsplit(":", 1)[0]
    if host in ("0.0.0.0", "::", "[::]"):
        return "wildcard"
    if text == owner_addr:
        return "owner"
    return "foreign"


def launch_unkeyed(args, declared):
    """The manifest-declared pair launched the way the deployment
    declares it — no `--pair-token`, each controller bound on its
    declared wildcard listen host with a runner-assigned port. The
    shared `pair.launch_pair` keys the pair for the announced-source
    contracts the switch legs exercise; this leg needs the unkeyed
    corner, where the standing claim's declared monitor is the only
    tracking source a demoted peer can prove. Returns the PairRig."""
    rig = pair.PairRig(declared)
    try:
        rig.plant, rig.plant_addr = pair.spawn_plant(
            args.plant_server, args.model, args.dynamics
        )
        rig.plant_io = simulate.PlantClient(rig.plant_addr)
        rig.duty, rig.duty_url, rig.duty_preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            None,
            rig.duty_files,
            listen=pair.listen_bind(rig.duty_decl),
            bound=rig.duty_bound,
        )
        if rig.duty_url is None:
            raise Abort(
                f"the duty controller {rig.duty_decl['name']} exited "
                f"at startup: "
                f"{'; '.join(rig.duty_preamble) or 'no diagnostic'}"
            )
        rig.standby, rig.standby_url, rig.standby_preamble = (
            pair.spawn_peer(
                args.controller,
                args.model,
                args.dt,
                rig.plant_addr,
                rig.duty_url.removeprefix("http://"),
                rig.standby_files,
                listen=pair.listen_bind(rig.standby_decl),
                bound=rig.standby_bound,
            )
        )
        if rig.standby_url is None:
            raise Abort(
                f"the standby controller {rig.standby_decl['name']} "
                "exited at startup: "
                f"{'; '.join(rig.standby_preamble) or 'no diagnostic'}"
            )
    except Exception:
        rig.close()
        raise
    return rig


def foreign_claim(foreign_io, tamper, evidence, label):
    """The monitor-less foreign preempt on the dedicated attachment:
    `claim_writer` under the leg's fixed token declaring no monitor.
    `controller: false` marks the honest hold a tool's — never an
    incumbent a peer's conditional grant must defer to — while the
    `phantom-release` tamper marks it a live controller's, so no
    conditional path can lift the claim the doctored release leaves
    standing. Returns the answer."""
    claim = foreign_io.request(
        {
            "op": "claim_writer",
            "owner": FOREIGN_CLAIM,
            "controller": tamper == "phantom-release",
        }
    )
    evidence[label + "_claim"] = claim
    result = claim.get("result")
    if result == "claimed_shared":
        raise Abort(
            f"the foreign claim answered claimed_shared — a live "
            f"attachment already holds the leg's fixed token "
            f"{FOREIGN_CLAIM:#x}: {claim}"
        )
    if claim_reclaim.unsupported_verb(claim):
        raise Inconclusive(
            "claim_writer answered invalid_request — the pinned "
            "release predates the claim lifecycle the leg stages",
            f"claim_writer answered {claim}",
        )
    if result != "done":
        raise Inconclusive(
            f"the foreign claim answered {result} — the consumer "
            "harness admits no unconditional preemption lever, or "
            "the pinned plant predates it",
            f"claim_writer answered {claim}",
        )
    return claim


def fenced_demotion(rig, ex_url, floor, failures, evidence, label):
    """The fenced-write demote-in-place watch on the fenced ex-owner:
    driven scans walk `active → demoting → standby` under the
    `fenced` origin beside one `field_claim_lost` attributed to the
    foreign token. Returns the digest's watch rows."""
    roles = []
    settled = None
    status, body = pair.request(f"{ex_url}/scan", {"scans": 1})
    if status != 200:
        if "fenc" in str(body):
            raise Inconclusive(
                "the superseded owner's fenced scan refused — the "
                "pinned release predates the demote-in-place "
                "contract the loss mark rides on",
                f"the first fenced scan answered {status} {body}",
            )
        failures.append(
            "the superseded owner's first fenced scan answered "
            f"{status} {body} — the preemption took the monitor "
            "down, not the degraded demote the contract settles"
        )
        raise Abort
    for _ in range(WATCH_SCANS):
        report = pair.get(f"{ex_url}/role", "GET /role", failures)
        roles.append(report.get("role"))
        if report.get("role") == "standby":
            settled = report
            break
        pair.scan(ex_url, failures)
    evidence[label + "_demotion"] = roles
    if settled is None:
        failures.append(
            "the superseded owner never demoted — the foreign claim "
            f"moved the claim but the role stayed {roles}"
        )
        raise Abort

    # The journaled loss: exactly one field_claim_lost above the
    # floor, attributed to the foreign token the field's own fencing
    # verdicts name, beside the fenced demote walk.
    journal = pair.get(f"{ex_url}/journal", "GET /journal", failures)
    added = journal[floor:]
    losses = stranded_rejoin.lost_entries(added)
    evidence[label + "_losses"] = losses
    if not losses:
        raise Inconclusive(
            "the fenced demotion left no field_claim_lost on the "
            "ex-owner's journal — the pinned release predates the "
            "loss record the contract's audit reads",
            f"the added journal carried {added}",
        )
    if len(losses) != 1:
        failures.append(
            f"expected exactly one field_claim_lost above the journal "
            f"floor, found {len(losses)} — one record per held claim, "
            "not one per fenced write"
        )
        raise Abort
    if "claimant" not in losses[0][1]:
        raise Inconclusive(
            "the journaled fencing loss names no claimant — the "
            "pinned release predates the attribution contract",
            f"the journaled loss was {losses}",
        )
    if losses[0][1].get("claimant") != FOREIGN_CLAIM:
        failures.append(
            "the journaled fencing loss attributes the takeover to "
            f"{losses[0][1].get('claimant')}, not the foreign token "
            f"{FOREIGN_CLAIM:#x}"
        )
        raise Abort
    walk = stranded_rejoin.role_walk(added)
    if any(origin is None for _from, _to, origin in walk):
        raise Inconclusive(
            "the fenced demote walk carries no switch origin — the "
            "pinned release predates the attribution fields",
            f"the role walk was {walk}",
        )
    if walk != [
        ("active", "demoting", "fenced"),
        ("demoting", "standby", "fenced"),
    ]:
        failures.append(
            f"the superseded owner's journaled role walk is {walk}, "
            "expected the fenced active → demoting → standby"
        )
        raise Abort
    return roles


def hold_window(
    rig,
    ctl,
    ex_url,
    sibling_url,
    verdict_io,
    plant_io,
    cmd,
    held0,
    failures,
    evidence,
    label,
):
    """The claim-standing window: while the monitor-less foreign
    claim stands, the fenced ex-owner must report `standby`/
    `unsynchronized` — and only that — the sibling stays an unarmed
    standby, the fencing verdict keeps naming the foreign owner with
    no monitor declared, the field records no write, and the shipped
    `dcs-plant-ctl` answers the unfenced census while its mutation
    meets the named fencing refusal. Returns the digest's hold rows."""
    hold = []
    ctl_seen = False
    for _ in range(HOLD_SCANS):
        pair.scan(ex_url, failures)
        pair.scan(sibling_url, failures)
        ex_role = pair.get(f"{ex_url}/role", "GET /role", failures)
        sibling_role = pair.get(
            f"{sibling_url}/role", "GET /role", failures
        )
        probe = verdict_io.request({"op": "step", "dt": 0})
        held = failover.field_read(plant_io, cmd, failures)["value"]
        sync = stranded_rejoin.sync_kind(ex_role)
        error = (probe or {}).get("error") or {}
        hold.append(
            {
                "ex": ex_role.get("role") + "/" + sync,
                "sibling": sibling_role.get("role")
                + "/"
                + stranded_rejoin.sync_kind(sibling_role),
                "claim": (
                    "foreign"
                    if claim_reclaim.verdict_owner(probe)
                    == FOREIGN_CLAIM
                    else "moved"
                ),
                "monitor": "absent" if "monitor" not in error else "named",
                "field_moved": held != held0,
            }
        )
        if sync == "missing":
            raise Inconclusive(
                "the fenced ex-owner's role report carries no sync "
                "vocabulary — the pinned release predates the "
                "contract's unsynchronized verdict",
                f"GET /role answered {ex_role}",
            )
        if ex_role.get("role") != "standby" or sync != "unsynchronized":
            failures.append(
                f"the fenced ex-owner reported "
                f"{ex_role.get('role')}/{sync} while the monitor-less "
                "foreign claim stood — `standby`/`unsynchronized` is "
                "the only honest report for that window: "
                f"{ex_role}"
            )
            raise Abort
        if sibling_role.get("role") != "standby":
            failures.append(
                "the sibling left standby under the held foreign "
                f"claim — the unarmed peer must stay standby: "
                f"{sibling_role}"
            )
            raise Abort
        if not claim_reclaim.mutation_fenced(probe) or (
            claim_reclaim.verdict_owner(probe) != FOREIGN_CLAIM
        ):
            failures.append(
                "the standing claim moved off the foreign token "
                f"during the hold window: {probe}"
            )
            raise Abort
        if "monitor" in error:
            failures.append(
                "a claim that declared no monitor names one in the "
                f"fencing verdict: {probe}"
            )
            raise Abort
        if held != held0:
            failures.append(
                "the field moved under the held foreign claim — a "
                f"foreign write landed: {held0} -> {held}"
            )
            raise Abort
        if not ctl_seen:
            ctl_step = plant_ctl(ctl, rig.plant_addr, "step", "0")
            ctl_list = plant_ctl(ctl, rig.plant_addr, "list")
            evidence[label + "_ctl_step"] = ctl_step[2].strip()
            if ctl_step[0] == 0 or FENCED_DETAIL not in ctl_step[2]:
                failures.append(
                    "a dcs-plant-ctl step under the held foreign "
                    "claim was not refused with the named fencing "
                    f"failure — exit {ctl_step[0]}: "
                    f"{ctl_step[2].strip() or ctl_step[1].strip()}"
                )
                raise Abort
            if ctl_list[0] != 0 or '"points"' not in ctl_list[1]:
                failures.append(
                    "dcs-plant-ctl list did not answer the field "
                    f"census under the held claim — exit "
                    f"{ctl_list[0]}: {ctl_list[2].strip()}"
                )
                raise Abort
            ctl_seen = True
    return hold


def release_claim(rig, foreign_io, tamper, evidence, label):
    """The claim's hand-back: `release_writer` on the attachment that
    holds it — or, under `phantom-release`, the doctored release
    issued through a fresh attachment that holds nothing, which the
    field rightly answers `done` while the standing claim survives,
    so the peer strands past the release the leg believes ran."""
    if tamper == "phantom-release":
        phantom = simulate.PlantClient(rig.plant_addr)
        try:
            release = phantom.request({"op": "release_writer"})
        finally:
            phantom.close()
    else:
        release = foreign_io.request({"op": "release_writer"})
    evidence[label + "_release"] = release
    if claim_reclaim.unsupported_verb(release):
        raise Inconclusive(
            "release_writer answered invalid_request — the pinned "
            "release predates the claim lifecycle the leg stages",
            f"release_writer answered {release}",
        )
    if tamper is None and release.get("result") != "done":
        raise Abort(f"the foreign claim's hand-back was refused: {release}")
    return release


def resolve_adoption(
    rig,
    ex_url,
    sibling_url,
    verdict_io,
    floor,
    peer_floor,
    ex_files,
    failures,
    evidence,
    label,
):
    """The declared-monitor resolution: the orphaned sibling's
    promote lands a controller-owned claim declaring its dialable
    monitor, and the fenced ex-owner resolves that claim-declared
    endpoint through the verified tracking path — the journaled
    `tracking_source_adopted` — converging `tracking`. Returns the
    resolution digest."""
    digest = {}
    sibling_port = stranded_rejoin.monitor_port(sibling_url)

    # The sibling's ownerless-line observation: pulling the demoted
    # ex-owner's checkpoint stamps the line unowned — `orphaned`,
    # the promotable posture the conditional orphan grant rides.
    observed = []
    orphaned = None
    for _ in range(WATCH_SCANS):
        report = pair.get(
            f"{sibling_url}/role", "GET /role", failures
        )
        observed.append(stranded_rejoin.sync_kind(report))
        if stranded_rejoin.sync_kind(report) == "orphaned":
            orphaned = report
            break
        pair.scan(sibling_url, failures)
    evidence[label + "_orphan"] = observed
    if orphaned is None:
        if any(kind == "missing" for kind in observed):
            raise Inconclusive(
                "the tracking peer's role report carries no sync "
                "vocabulary — the pinned release predates the "
                "contract's orphaned verdict",
                f"the sibling's sync readings were {observed}",
            )
        raise Inconclusive(
            "the tracking peer never reported the orphaned line — "
            "the pinned release predates the ownerless-line verdict "
            "the adoption resolution rides",
            f"the sibling's sync readings were {observed}",
        )
    digest["orphan"] = "reported"
    peer_orphans = [
        entry["event"]["field_orphaned"]
        for entry in pair.get(
            f"{sibling_url}/journal", "GET /journal", failures
        )[peer_floor:]
        if "field_orphaned" in entry.get("event", {})
    ]
    if len(peer_orphans) != 1:
        raise Inconclusive(
            "the orphaned observation left no field_orphaned record "
            "on the tracking peer's journal — the pinned release "
            "predates the journaled orphan contract",
            f"the sibling journaled {peer_orphans}",
        )
    digest["orphan_journaled"] = len(peer_orphans)

    # The successor's conditional claim: POST /promote on the
    # orphaned peer, its first scan landing the claim under its own
    # token declaring its monitor — the field-arbitrated successor
    # the released contract names.
    report = rig.promote(
        sibling_url,
        failures,
        "the orphaned sibling under the released field",
    )
    digest["promote"] = "granted"
    seated = None
    roles = []
    for _ in range(WATCH_SCANS):
        pair.scan(sibling_url, failures)
        report = pair.get(
            f"{sibling_url}/role", "GET /role", failures
        )
        roles.append(report.get("role"))
        if report.get("role") == "active":
            seated = report
            break
    evidence[label + "_seated"] = roles
    if seated is None:
        digest["seated"] = "refused"
        failures.append(
            "the promoted sibling never settled active — its "
            "conditional claim on the released field was refused: "
            f"{roles}"
        )
        return digest
    digest["seated"] = "active"

    verdict = verdict_io.request({"op": "step", "dt": 0})
    evidence[label + "_verdict"] = verdict
    owner = claim_reclaim.verdict_owner(verdict)
    if owner is None:
        raise Inconclusive(
            "the post-resolution fencing verdict names no standing "
            "owner — the pinned release predates the verdict "
            "attribution the contract's claimants read",
            f"the fencing verdict was {verdict}",
        )
    if owner == FOREIGN_CLAIM or not claim_reclaim.mutation_fenced(
        verdict
    ):
        digest["seated_claim"] = "foreign"
        failures.append(
            "the released field stands under the foreign token past "
            f"the sibling's promote: {verdict}"
        )
        return digest
    declared = stranded_rejoin.verdict_monitor(verdict)
    if declared is None:
        raise Inconclusive(
            "the successor's claim declares no monitor — the pinned "
            "release predates the claim-declared monitor the "
            "adoption resolution reads",
            f"the fencing verdict was {verdict}",
        )
    kind = monitor_kind(declared, monitor_addr(sibling_url))
    if kind == "wildcard":
        raise Inconclusive(
            "the successor's claim declares its wildcard bind "
            "verbatim — the pinned release predates the "
            "claim-monitor normalization the adoption needs",
            f"the declared monitor was {declared}",
        )
    if kind != "owner":
        digest["seated_claim"] = "misnamed"
        failures.append(
            f"the successor's claim declares monitor {declared} — "
            "not the sibling's dialable monitor "
            f"{monitor_addr(sibling_url)}"
        )
        return digest
    digest["seated_claim"] = "named"

    # The adoption: the ex-owner's claim probes now meet the verdict
    # naming the successor's declared monitor — the only provable
    # rendezvous on the unkeyed run — and the verified tracking pull
    # converges it `unsynchronized` → `tracking`, journaled.
    rejoin = []
    tracked = None
    for _ in range(RESOLVE_SCANS):
        pair.scan(ex_url, failures)
        report = pair.get(f"{ex_url}/role", "GET /role", failures)
        kind = stranded_rejoin.sync_kind(report)
        rejoin.append(report.get("role") + "/" + kind)
        if claim_reclaim.tracking(report):
            tracked = report
            break
        if report.get("role") != "standby" or kind not in (
            "unsynchronized",
        ):
            failures.append(
                f"the fenced ex-owner reported "
                f"{report.get('role')}/{kind} resolving the released "
                "field — before the claim-declared successor verifies, "
                "unsynchronized is the only honest report: "
                f"{report}"
            )
            raise Abort
    evidence[label + "_rejoin"] = rejoin
    if tracked is None:
        digest["rejoin"] = "wedged"
        failures.append(
            "the released field never resolved — the fenced ex-owner "
            "never adopted the successor's declared monitor; its "
            f"reports stayed {rejoin}"
        )
        return digest
    digest["rejoin"] = "tracked"

    # The journaled adoption: one tracking_source_adopted naming the
    # declared monitor's endpoint, beside the fenced demote walk —
    # the served journal and the manifest-declared durable file both.
    journal = pair.get(f"{ex_url}/journal", "GET /journal", failures)
    adoptions = stranded_rejoin.adopted_entries(journal[floor:])
    evidence[label + "_adopted"] = adoptions
    if len(adoptions) != 1:
        digest["adopted"] = "silent"
        failures.append(
            f"the ex-owner journaled {len(adoptions)} "
            "tracking_source_adopted records for the adoption — the "
            "claim-declared rendezvous must journal once"
        )
    elif not str(adoptions[0][1].get("source")).endswith(
        ":" + sibling_port
    ):
        digest["adopted"] = "foreign"
        failures.append(
            f"the adopted source {adoptions[0][1].get('source')} does "
            f"not resolve the claim-declared monitor on "
            f":{sibling_port}"
        )
    else:
        digest["adopted"] = "resolved"
    peer_walk = stranded_rejoin.role_walk(
        pair.get(f"{sibling_url}/journal", "GET /journal", failures)[
            peer_floor:
        ]
    )
    if peer_walk != [
        ("standby", "promoting", "request"),
        ("promoting", "active", "request"),
    ]:
        failures.append(
            "the promoted sibling's journaled role walk is "
            f"{peer_walk}, expected the request-origin "
            "standby → promoting → active"
        )
    journal_file = ex_files.get("journal_file")
    if journal_file is None:
        raise Inconclusive(
            "the manifest's pair declares no journal_file on the "
            "ex-owner — the durable half of the adoption audit is "
            "absent"
        )
    kinds = stranded_rejoin.durable_kinds(journal_file)
    evidence[label + "_durable"] = sorted(kinds)
    for owed in ("field_claim_lost", "tracking_source_adopted"):
        if owed not in kinds:
            failures.append(
                f"the ex-owner's durable journal file carries no "
                f"{owed} record — the journaled evidence the "
                "release-resolution contract owes"
            )
    if failures:
        raise Abort

    # The pair's settle after the adoption: driven tracking-first
    # ticks prove the ex-owner's images identical to the successor
    # owner's — the pair reconverged, no peer left stranded.
    ticks = []
    for _ in range(pair.HANDOVER_TICKS):
        _tracked, owner = rig.tick(
            ex_url,
            sibling_url,
            failures,
            diverged="the adopted ex-owner's image diverged from "
            "the successor's at tick {tick} — the resolution was "
            "not bumpless",
        )
        ticks.append(owner["tick"])
    digest["settled"] = ticks
    return digest


def resolve_reclaim(
    rig,
    ex_url,
    sibling_url,
    verdict_io,
    floor,
    owner_token,
    failures,
    evidence,
    label,
):
    """The recorded-reclaim resolution: driven scans on the fenced
    ex-owner re-arm its bound conditional grant under its recorded
    token once the field stands unclaimed — the unattended
    `standby → promoting → active` walk under the `reclaim` origin —
    the released field re-seated to the standing owner, never
    stranded. Returns the resolution digest."""
    digest = {}
    watch = []
    promoted = None
    for _ in range(RESOLVE_SCANS):
        pair.scan(ex_url, failures)
        report = pair.get(f"{ex_url}/role", "GET /role", failures)
        watch.append(
            report.get("role") + "/" + stranded_rejoin.sync_kind(report)
        )
        if report.get("role") == "active":
            promoted = report
            break
        if report.get("role") not in ("standby", "promoting"):
            failures.append(
                f"the fenced ex-owner reported "
                f"{report.get('role')} resolving the released field "
                "— it may only walk back toward active: "
                f"{report}"
            )
            raise Abort
    evidence[label + "_reclaim"] = watch
    if promoted is None:
        digest["reclaim"] = "stranded"
        failures.append(
            "the released field never resolved — the fenced ex-owner "
            "never re-armed its recorded claim past the claim's "
            f"release; its reports stayed {watch}"
        )
        return digest
    digest["reclaim"] = "active"

    # The re-armed claim must name the standing owner's own token —
    # the recorded reclaim — and declare its dialable monitor again.
    verdict = verdict_io.request({"op": "step", "dt": 0})
    evidence[label + "_verdict"] = verdict
    if not claim_reclaim.mutation_fenced(verdict):
        failures.append(
            "the re-seated claim does not fence third-party "
            f"mutations: {verdict}"
        )
        return digest
    if claim_reclaim.verdict_owner(verdict) != owner_token:
        digest["owner"] = "foreign"
        failures.append(
            "the released claim did not re-arm to the recorded "
            f"owner — the fencing verdict names "
            f"{claim_reclaim.verdict_owner(verdict)}: {verdict}"
        )
        return digest
    digest["owner"] = "owner"
    declared = stranded_rejoin.verdict_monitor(verdict)
    if declared is None:
        raise Inconclusive(
            "the re-armed claim declares no monitor — the pinned "
            "release predates the claim-declared monitor the "
            "contract carries",
            f"the fencing verdict was {verdict}",
        )
    kind = monitor_kind(declared, monitor_addr(ex_url))
    if kind == "wildcard":
        raise Inconclusive(
            "the re-armed claim declares its wildcard bind verbatim "
            "— the pinned release predates the claim-monitor "
            "normalization",
            f"the declared monitor was {declared}",
        )
    if kind != "owner":
        digest["monitor"] = "misnamed"
        failures.append(
            f"the re-armed claim declares monitor {declared} — not "
            f"the ex-owner's dialable monitor {monitor_addr(ex_url)}"
        )
        return digest
    digest["monitor"] = "owner"

    # The unattended reclaim's journaled walk back to active — under
    # the `reclaim` origin, no operator call having run.
    journal = pair.get(f"{ex_url}/journal", "GET /journal", failures)
    walk = stranded_rejoin.role_walk(journal[floor:])
    evidence[label + "_walk"] = walk
    if ("standby", "promoting", "reclaim") not in walk or (
        "promoting",
        "active",
        "reclaim",
    ) not in walk:
        failures.append(
            "the released field's reclaim left no journaled walk "
            "back to active under the `reclaim` origin — an operator "
            f"or restart path ran instead: {walk}"
        )
        raise Abort
    digest["walk"] = "reclaim"

    # The pair's settle after the reclaim: the orphaned-tracking
    # sibling re-joins the re-seated owner — driven tracking-first
    # ticks prove identical images while its report returns to
    # `tracking`, no peer left stranded.
    ticks = []
    for _ in range(pair.HANDOVER_TICKS):
        _tracked, owner = rig.tick(
            sibling_url,
            ex_url,
            failures,
            diverged="the sibling's image diverged from the "
            "reclaimed owner's at tick {tick} — the reclaim was not "
            "bumpless",
        )
        ticks.append(owner["tick"])
    sibling_role = pair.get(
        f"{sibling_url}/role", "GET /role", failures
    )
    if not claim_reclaim.tracking(sibling_role):
        failures.append(
            "the sibling never rejoined the reclaimed owner — GET "
            f"/role answers {sibling_role}"
        )
        raise Abort
    digest["settled"] = ticks
    return digest


def foreign_claim_release_pass(args, tamper):
    """The foreign-claim-release run: launch the unkeyed declared-binds
    pair, converge, gate the contract surface, then per episode the
    monitor-less foreign claim, the bounded `unsynchronized` window,
    the release, and the resolution — the successor's declared
    monitor adoption first, the ex-owner's recorded reclaim after the
    launch roles restore. Returns `(digest_entries, evidence,
    failures)`; raises `Inconclusive` where the pinned release
    predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "foreign-claim-release leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    rig = verdict_io = foreign_io = None
    try:
        rig = launch_unkeyed(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io
        # The claim attachments the episode stages through — the
        # rig's own client stays read-only: `verdict_io` runs the
        # third-party mutation probes whose fencing verdicts carry
        # the standing claim's declared monitor, and `foreign_io`
        # holds the monitor-less foreign claim the window asserts.
        verdict_io = simulate.PlantClient(rig.plant_addr)
        foreign_io = simulate.PlantClient(rig.plant_addr)
        with open(args.model) as handle:
            model = json.load(handle)
        points = failover.signal_points(model)
        if points is None:
            raise Abort(
                "the emitted model declares no p101-cmd/level-primary "
                "signal points — the leg has no field point to watch"
            )
        cmd = points["cmd"]

        # Phase 1 — the declared wildcard binds deployed verbatim:
        # the unkeyed corner's own deployment shape, where a claim
        # declaring its bind address is the undialable declaration
        # the normalization must substitute.
        binds = []
        for name, entry, bound in (
            (duty_decl["name"], duty_decl, rig.duty_bound),
            (standby_decl["name"], standby_decl, rig.standby_bound),
        ):
            declared_host = entry.get("listen", "").rsplit(":", 1)[0]
            bound_host = bound[0].rsplit(":", 1)[0] if bound else None
            binds.append(declared_host)
            if bound_host != declared_host:
                failures.append(
                    f"{name} did not bind its declared listen host "
                    f"{declared_host} — the spawn reported "
                    f"{bound or 'no listener'}"
                )
        if failures:
            raise Abort

        # Phase 2 — convergence and the contract surface: the
        # launched active's recorded claim token, the standing claim
        # fencing third-party mutations while naming its owner and
        # its declared dialable monitor, the lifecycle verbs
        # answered, the checkpoint's ownership stamps, and the
        # durable journal files the manifest declares. Each absence
        # is the release predating the contract, never a violation.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "binds": binds,
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )
        owner_token = failover.owner_token(rig.duty_preamble)
        if owner_token is None:
            raise Inconclusive(
                "the launched active recorded no claim line — the "
                "pinned release claims only on promotion, predating "
                "the claim lifecycle the release-resolution contract "
                "rides on"
            )
        if rig.duty_files.get("journal_file") is None or (
            rig.standby_files.get("journal_file") is None
        ):
            raise Inconclusive(
                "the manifest's pair declares no journal files — "
                "the durable half of the resolution audit is absent"
            )
        probe0 = verdict_io.request({"op": "step", "dt": 0})
        evidence["probe0"] = probe0
        if not claim_reclaim.mutation_fenced(probe0):
            failures.append(
                "the field held no writer claim after convergence — "
                f"a third-party probe answered {probe0}, so the leg "
                "has no standing claim to lose"
            )
            raise Abort
        if claim_reclaim.verdict_owner(probe0) is None:
            raise Inconclusive(
                "the fencing verdict names no standing owner — the "
                "pinned release predates the verdict attribution "
                "the contract's claimants read",
                f"the standing fencing verdict was {probe0}",
            )
        if claim_reclaim.verdict_owner(probe0) != owner_token:
            failures.append(
                "the standing claim names a foreign token, not the "
                f"launch owner — the pair is not in its launch claim "
                f"state: {probe0}"
            )
            raise Abort
        declared0 = stranded_rejoin.verdict_monitor(probe0)
        if declared0 is None:
            raise Inconclusive(
                "the standing claim declares no monitor — the pinned "
                "release predates the claim-declared monitor the "
                "release resolution reads",
                f"the standing fencing verdict was {probe0}",
            )
        kind0 = monitor_kind(declared0, monitor_addr(duty_url))
        if kind0 == "wildcard":
            raise Inconclusive(
                "the standing claim declares its wildcard bind "
                "verbatim — the pinned release predates the "
                "claim-monitor normalization",
                f"the declared monitor was {declared0}",
            )
        if kind0 != "owner":
            failures.append(
                f"the standing claim declares monitor {declared0} — "
                "not the field owner's dialable monitor "
                f"{monitor_addr(duty_url)}"
            )
            raise Abort
        doc = pair.get(
            f"{duty_url}/checkpoint", "GET /checkpoint", failures
        )
        if "source_owns_field" not in doc or "line_owner" not in doc:
            raise Inconclusive(
                "the served checkpoint carries no field-ownership "
                "stamps — the pinned release predates the "
                "field-arbitrated monitor contract",
                f"the checkpoint serves {sorted(doc)}",
            )
        held0 = failover.field_read(plant_io, cmd, failures)["value"]
        digest_entries.append(
            {
                "phase": "gate",
                "probe": "fenced",
                "owner": "named",
                "monitor": kind0,
            }
        )

        def episode(label, resolution):
            """One claim-hold-release episode's shared prefix: the
            foreign preempt, the fenced demotion, and the bounded
            `unsynchronized` window — returning the episode's journal
            floors and digest entries."""
            journal = pair.get(
                f"{duty_url}/journal", "GET /journal", failures
            )
            floor = len(journal)
            peer_floor = len(
                pair.get(
                    f"{standby_url}/journal", "GET /journal", failures
                )
            )
            foreign_claim(foreign_io, tamper, evidence, label)
            seized = verdict_io.request({"op": "step", "dt": 0})
            evidence[label + "_seized"] = seized
            if not claim_reclaim.mutation_fenced(seized):
                failures.append(
                    "the foreign claim's preempt did not fence the "
                    f"field — a third-party probe answered {seized}"
                )
                raise Abort
            if claim_reclaim.verdict_owner(seized) != FOREIGN_CLAIM:
                if claim_reclaim.verdict_owner(seized) is None:
                    raise Inconclusive(
                        "the post-preemption fencing verdict names "
                        "no standing owner — the pinned release "
                        "predates the verdict attribution",
                        f"the fencing verdict was {seized}",
                    )
                failures.append(
                    "the fencing verdict attributes the preempted "
                    "claim to "
                    f"{claim_reclaim.verdict_owner(seized)}, not the "
                    f"foreign token {FOREIGN_CLAIM:#x}: {seized}"
                )
                raise Abort
            if "monitor" in ((seized.get("error")) or {}):
                failures.append(
                    "the monitor-less foreign claim names a monitor "
                    f"in the fencing verdict: {seized}"
                )
                raise Abort
            roles = fenced_demotion(
                rig, duty_url, floor, failures, evidence, label
            )
            hold = hold_window(
                rig,
                args.plant_ctl,
                duty_url,
                standby_url,
                verdict_io,
                plant_io,
                cmd,
                held0,
                failures,
                evidence,
                label,
            )
            digest_entries.append(
                {
                    "phase": label + "-claim",
                    "seized": failover.probe_kind(seized),
                    "watch": roles,
                    "hold": hold,
                }
            )
            release_claim(rig, foreign_io, tamper, evidence, label)
            digest_entries.append(
                {"phase": label + "-release", "release": "done"}
            )
            if resolution == "adoption":
                digest = resolve_adoption(
                    rig,
                    duty_url,
                    standby_url,
                    verdict_io,
                    floor,
                    peer_floor,
                    rig.duty_files,
                    failures,
                    evidence,
                    label,
                )
            else:
                digest = resolve_reclaim(
                    rig,
                    duty_url,
                    standby_url,
                    verdict_io,
                    floor,
                    owner_token,
                    failures,
                    evidence,
                    label,
                )
            digest_entries.append(
                {"phase": label + "-resolve", **digest}
            )
            if failures:
                raise Abort

        # Episode A — the recorded reclaim: the monitor-less hold,
        # the release, and the ex-owner's own bound re-grant
        # re-seating the released field under its recorded token —
        # run first so the fenced peer still carries no learned
        # tracking pin and the `unsynchronized` window is strict.
        episode("reclaimed", "reclaim")

        # Episode B — the declared-monitor adoption: the same
        # monitor-less hold, the release, and the orphaned sibling's
        # promote re-seating the released field under a controller
        # claim declaring its dialable monitor — landed before the
        # ex-owner's next scan so its own bound re-grant probe meets
        # the successor's standing claim — the fenced ex-owner then
        # resolving the claim-declared endpoint through the verified
        # tracking path.
        episode("adopted", "adoption")

        # The restore: the documented switch seats the launch owner
        # back on the field — demote the successor, promote the
        # tracked ex-owner — so the run ends on the pair's launch
        # roles and claim state.
        restored = rig.switch(
            standby_url,
            duty_url,
            failures,
            demote_what="the successor under the released field",
            promote_what="the tracked ex-owner",
        )
        digest_entries.append(
            {
                "phase": "restore",
                "ticks": restored["ticks"],
                "duty_role": "active",
                "standby_role": "tracking",
            }
        )

        # The reconverge: driven tracking-first ticks prove the
        # restored pair keeps identical images — the launch roles
        # stand: the manifest-declared duty controller `active`, its
        # standby `tracking`, the claim under the owner's own token.
        handover = []
        for _ in range(pair.HANDOVER_TICKS):
            _tracked, owner = rig.tick(
                standby_url,
                duty_url,
                failures,
                diverged="the restored pair's images diverged at "
                "tick {tick} — the release resolution was not "
                "bumpless",
            )
            handover.append(owner["tick"])
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active" or (
            not claim_reclaim.tracking(standby_role)
        ):
            failures.append(
                "the pair did not reconverge to its launch roles — "
                f"{duty_role} / {standby_role}"
            )
            raise Abort
        held = failover.field_read(plant_io, cmd, failures)["value"]
        if held != simulate.snapshot_point(owner, cmd):
            failures.append(
                "the re-seated claim fences the owner's own writes "
                f"— the field carries {held} while its image "
                f"reports {simulate.snapshot_point(owner, cmd)}"
            )
            raise Abort
        evidence["final_tick"] = handover[-1]

        # The durable mirror: the manifest-declared journal file on
        # the ex-owner carries both episodes' records — the two
        # attributed losses, the declared-monitor adoption, and the
        # request/reclaim role walks — in file order.
        durable = stranded_rejoin.journal_entries(
            rig.duty_files["journal_file"]
        )
        durable_losses = stranded_rejoin.lost_entries(durable)
        evidence["durable_losses"] = durable_losses
        if len(durable_losses) != 2 or any(
            record.get("claimant") != FOREIGN_CLAIM
            for _seq, record in durable_losses
        ):
            failures.append(
                "the durable journal carries the losses "
                f"{durable_losses} — expected two field_claim_lost "
                "records attributed to the foreign token, one per "
                "episode"
            )
            raise Abort
        durable_kinds = stranded_rejoin.durable_kinds(
            rig.duty_files["journal_file"]
        )
        if "tracking_source_adopted" not in durable_kinds:
            failures.append(
                "the durable journal file carries no "
                "tracking_source_adopted record — the declared-"
                "monitor adoption went unrecorded"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "reconverge",
                "ticks": handover,
                "duty_role": "active",
                "standby_role": "tracking",
                "durable": sorted(durable_kinds),
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        # Detach the claim attachments cleanly: release whatever
        # hold each still carries — a hold left standing keeps the
        # field claimed for a dead token — then close. The release
        # drops only the connection's own hold, so it never takes
        # the owner's claim down with it.
        for client in (verdict_io, foreign_io):
            if client is not None:
                try:
                    client.request({"op": "release_writer"})
                except Exception:
                    pass
                client.close()
        if rig is not None:
            rig.close()
    return digest_entries, evidence, failures


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plant-server", required=True)
    parser.add_argument("--controller", required=True)
    parser.add_argument("--plant-ctl", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--tamper",
        choices=["phantom-release"],
        help="doctor the claim's hand-back — the release lands on a "
        "non-holder while the claim stands as a live controller "
        "incumbent, so the peer strands past the claim's release "
        "and the leg must report the named diagnostic",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = (
            foreign_claim_release_pass(args, args.tamper)
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "foreign-claim-release: the doctored hand-back "
                "wanted the peer stranded past the claim's release "
                "— an inconclusive run offers the doctored case no "
                "evidence"
            )
            return 1
        # The digest line carries only the stable reason — the
        # run's own verdicts and endpoints report on stderr, where
        # two identical passes need not share them.
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"foreign-claim-release: inconclusive — {detail}")
        print(
            f"foreign-claim-release-digest inconclusive — {reason}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"foreign-claim-release: {line}")
        return 1
    for failure in failures:
        eprint(f"foreign-claim-release: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"foreign-claim-release: the {args.tamper} case "
                "passed silently — the leg never noticed the "
                "doctored hand-back"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"foreign-claim-release-digest {digest} — the held "
        "monitor-less foreign claim kept the fenced ex-owner "
        "unsynchronized for its standing window only, the released "
        "field resolved through the declared-monitor adoption and "
        "the recorded reclaim, journaled, and the launch roles "
        f"stand at tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
