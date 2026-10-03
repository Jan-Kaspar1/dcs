#!/usr/bin/env python3
"""The stranded-standby re-join leg for the reference plant — the
consumer-side proof that a tracking standby promoted while the field
owner still runs leaves the quiesced ex-owner re-joined, not
stranded: its first fenced write demotes it in place, the fencing
verdict names the successor's declared monitor — the
field-arbitrated tracking candidate decision 101 records — and the
demoted peer resolves it through the verified tracking path,
journals the attributed `field_claim_lost` and
`tracking_source_adopted`, and reports `tracking` inside a bounded
scan window (WW-ENG-003, WW-LCM-001 — the #1042 stranded-standby
fix and the #1045 monitor-less window amendment, mirrored at the
customer boundary from the qa rig's `stranded-standby-no-resync`
scenario).

The demote-reconvergence leg (`ci/legs/demote_reconvergence.py`)
proves the documented switch — `POST /demote` on the owner first —
adopts the announced source; the claim-reclaim leg proves the
foreign preempt-and-release lifecycle. This leg drives the
involuntary entry the finding names — `POST /promote` on the
tracking standby with no `POST /demote` on the owner first — on the
manifest-declared pair, twice, once per direction:

- the launch owner's demotion owes a journaled adoption — it has no
  configured `--standby` source, so the standing claim's declared
  monitor is the only candidate its re-join can resolve: the
  fencing verdict naming the promoted peer's monitor, the
  `tracking_source_adopted` record naming the same endpoint, the
  `tracking` report inside the bound;
- the sibling's demotion re-joins through its configured source —
  no adoption journal owed, its record only audited for
  consistency where it appears;
- the #1045 window: a held tool claim declaring no monitor
  (`claim_writer` with `controller: false`) demotes the field owner
  the same way but leaves the demoted peer un-converged only while
  the claim stands — parked `unsynchronized`/`orphaned`, never
  `tracking` — until the sibling's promote re-seats the field
  under a controller-owned claim that declares a monitor and the
  demoted peer converges;
- both directions promotable: the settled pair reports exactly one
  active — the promoted peer — with the demoted peer tracking it,
  each cycle's recorded vocabulary landing in the manifest-declared
  durable journal file, and the launch roles restored.

The contract postdates the pinned release line: where the launched
tooling predates it — no recorded owner-token claim line, the claim
lifecycle verbs unanswered, a fencing verdict naming no standing
owner or no declared monitor, a served checkpoint without the
`source_owns_field`/`line_owner` stamps, or the journaled loss
carrying no claimant — the run's own evidence is the pre-contract
shape and the leg reports `stranded-rejoin-digest inconclusive`
rather than asserting until the manifest repins a release carrying
the contract.

Usage:

    stranded_rejoin.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `stranded-rejoin-digest <sha256>` line prints — the
check runs two passes and compares them
(`stranded-rejoin-nondeterministic`). A contract violation reports
`stranded-rejoin: …` lines on stderr and exits 1 — the check's
`stranded-rejoin-failed`. `--tamper expect-wedge` doctors the leg's
own expectation to the pre-contract shape — asserting the demoted
peer may stay un-converged, the stranded-standby wedge the finding
names — so the leg proves its re-join assertion fires on the
honest reconvergence rather than passing an unexercised contract.
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import claim_reclaim
import demote_reconvergence
import failover
import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the demoted peer may stay
# un-converged — the stranded-standby wedge the contract closed —
# must surface the named diagnostic on the honest re-join rather
# than passing an unexercised contract.
LEG = {
    "order": 420,
    "title": "the stranded-standby re-join leg",
    "passes": "stranded-rejoin-leg",
    "tampers": [
        {
            "name": "expect-wedge",
            "passed": "an expect-wedge case passed the stranded-rejoin leg",
            "missed": "the expect-wedge case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted the demoted peer stranded"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates — or never covers — the contract
    the leg exercises: the run classifies inconclusive, never a
    product failure."""


# The driven-scan bounds the episode's phases run: the superseded
# owner's demote-in-place settles inside a couple of scans, the
# re-join is the bounded window the contract names — the finding's
# indefinite wedge is the failure the bound catches — and the
# monitor-less hold window spans a handful of driven scans.
WATCH_SCANS = 6
REJOIN_SCANS = 12
HOLD_SCANS = 3
RESTORE_SCANS = 12
HOLD_TICKS = 3

# The monitor-less tool claim's foreign owner token — a small fixed
# value that cannot collide with a controller's per-process minted
# token, distinct from the tokens the other legs stage.
TOOL_CLAIM = 0xF031


def journal_entries(path):
    """A `--journal-file`'s entry records in file order — the durable
    half of the audit."""
    return [
        record
        for kind, record in pair.journal_records(path)
        if kind == "entry"
    ]


def durable_kinds(path):
    """The event kinds a `--journal-file`'s entry records carry."""
    return {
        kind
        for entry in journal_entries(path)
        for kind in entry.get("event", {})
    }


def lost_entries(entries):
    """The `field_claim_lost` records of a journal entry list —
    `(seq, record)` pairs in journal order."""
    return [
        (entry["seq"], entry["event"]["field_claim_lost"])
        for entry in entries
        if "field_claim_lost" in entry.get("event", {})
    ]


def adopted_entries(entries):
    """The `tracking_source_adopted` records of a journal entry list
    — `(seq, record)` pairs in journal order."""
    return [
        (entry["seq"], entry["event"]["tracking_source_adopted"])
        for entry in entries
        if "tracking_source_adopted" in entry.get("event", {})
    ]


def role_walk(entries):
    """The `role_changed` stream of a journal entry list —
    `(from, to, origin)` per entry, in `seq` order; the origin reads
    `None` on an entry predating the attribution fields."""
    return [
        (change["from"], change["to"], change.get("origin"))
        for entry in entries
        if "role_changed" in entry.get("event", {})
        for change in [entry["event"]["role_changed"]]
    ]


def monitor_port(url):
    """The served monitor's port — the endpoint suffix a declared
    monitor, an adopted source, or a propagated `line_owner` must
    resolve to."""
    return url.rsplit(":", 1)[-1]


def verdict_monitor(verdict):
    """The declared monitor a fencing verdict names — the `monitor`
    field the plant's `fenced` answers carry under the
    claim-declared-monitor contract — or None where the release
    predates the field."""
    return ((verdict or {}).get("error") or {}).get("monitor")


def demote_walk(entries):
    """The fenced demote-in-place role walk the superseded owner's
    journal carries above the episode floor."""
    return [
        ("active", "demoting", "fenced"),
        ("demoting", "standby", "fenced"),
    ] == role_walk(entries)


def promote_walk(entries):
    """The request-origin promote walk the promoted peer's journal
    carries above the episode floor."""
    return [
        ("standby", "promoting", "request"),
        ("promoting", "active", "request"),
    ] == role_walk(entries)


def sync_kind(report):
    """The sync vocabulary a standby's RoleReport carries —
    `tracking`, `orphaned`, `degraded`, `diverged` — or
    `unsynchronized` when the report carries the string form."""
    sync = (report or {}).get("sync")
    if isinstance(sync, str):
        return sync
    return claim_reclaim.sync_state(report) or "missing"


def rejoin_cycle(
    rig,
    demoted_url,
    promoted_url,
    demoted_files,
    adoption_owed,
    verdict_io,
    failures,
    evidence,
    label,
):
    """One involuntary-demote cycle: `POST /promote` on the tracking
    peer `promoted_url` while `demoted_url` still owns the field —
    never `POST /demote` on the owner first. Asserts the recorded
    contract on the demoted peer: the demote-in-place walk, the
    attributed `field_claim_lost`, the verdict naming the
    successor's declared monitor, the bounded `tracking` re-join,
    the journaled `tracking_source_adopted` naming the declared
    endpoint where one is owed, the honestly stamped re-joined
    document, and the settled pair shape. `adoption_owed` is set for
    the launch owner — the peer the contract names, whose only
    tracking candidates the claim and the announced hints supply;
    the configured-source sibling re-joins without an owed
    adoption. Returns the cycle's digest dict."""
    digest = {}
    demoted_port = monitor_port(demoted_url)
    promoted_port = monitor_port(promoted_url)
    floor = len(
        pair.get(f"{demoted_url}/journal", "GET /journal", failures)
    )
    peer_floor = len(
        pair.get(f"{promoted_url}/journal", "GET /journal", failures)
    )

    # The involuntary entry: promote the tracking peer with the
    # owner still running — the finding's misordered switch.
    status, report = pair.request(f"{promoted_url}/promote", {})
    evidence[label + "_promote"] = {"status": status, "body": report}
    if status != 200 or report.get("role") != "promoting":
        digest["promote"] = "refused"
        failures.append(
            f"POST /promote on the tracking peer answered {status} "
            f"{report} — the involuntary entry must answer a "
            "promoting report"
        )
        return digest
    digest["promote"] = "granted"

    # The demote-in-place watch: the promoted peer's first scan
    # lands its claim — the promoted-first scan order the tracking
    # pull train needs — and the superseded owner's own fenced write
    # demotes it in place, monitor answering throughout.
    walk = []
    settled = None
    for _ in range(WATCH_SCANS):
        pair.scan(promoted_url, failures)
        pair.scan(demoted_url, failures)
        report = pair.get(f"{demoted_url}/role", "GET /role", failures)
        walk.append(report.get("role"))
        if report.get("role") == "standby":
            settled = report
            break
    evidence[label + "_demotion"] = walk
    if settled is None:
        digest["demotion"] = "held"
        failures.append(
            "the superseded peer never demoted in place — the "
            f"promoted peer took the claim but the role walk "
            f"stayed {walk}"
        )
        return digest
    if any(
        role not in ("active", "demoting", "standby") for role in walk
    ):
        failures.append(
            f"the superseded peer reported an unexpected role walk "
            f"{walk} — the demote-in-place walks only "
            "active → demoting → standby"
        )
    digest["demotion"] = "in-place"
    demote_tick = settled.get("tick")

    # The standing claim's declared monitor — the field-arbitrated
    # candidate the decision names — on a fresh third-party probe.
    probe = verdict_io.request({"op": "step", "dt": 0})
    evidence[label + "_verdict"] = probe
    owner = claim_reclaim.verdict_owner(probe)
    declared = verdict_monitor(probe)
    if not claim_reclaim.mutation_fenced(probe):
        digest["declared"] = "unclaimed"
        failures.append(
            "the promoted peer's claim does not fence third-party "
            f"mutations: {probe}"
        )
        return digest
    if owner is None:
        raise Inconclusive(
            "the post-promotion fencing verdict names no standing "
            "owner — the pinned release predates the verdict "
            f"attribution the loss's claimant reads: {probe}"
        )
    if declared is None:
        digest["declared"] = "absent"
        failures.append(
            "the promoted peer's claim declares no monitor — the "
            f"field-arbitrated tracking candidate is missing: {probe}"
        )
    elif not str(declared).endswith(":" + promoted_port):
        digest["declared"] = "misnamed"
        failures.append(
            f"the declared monitor {declared} does not resolve the "
            f"promoted peer on :{promoted_port}"
        )
    else:
        digest["declared"] = "named"
    if failures:
        return digest

    # The journaled loss the demotion owes — exactly one
    # field_claim_lost attributed to the promoted peer's claim —
    # beside the fenced demote walk.
    journal = pair.get(f"{demoted_url}/journal", "GET /journal", failures)
    added = journal[floor:]
    losses = lost_entries(added)
    evidence[label + "_losses"] = losses
    if not losses:
        digest["loss"] = "silent"
        failures.append(
            "the demoted peer journaled no field_claim_lost during "
            "the supersede"
        )
    elif len(losses) != 1:
        digest["loss"] = "duplicated"
        failures.append(
            f"the demoted peer journaled {len(losses)} "
            "field_claim_lost records for one supersede — one "
            "record per held claim, not one per fenced write"
        )
    elif "claimant" not in losses[0][1]:
        raise Inconclusive(
            "the journaled field_claim_lost carries no claimant — "
            "the loss attribution the episode's evidence reads; "
            f"the pinned release predates the contract: {losses}"
        )
    elif losses[0][1].get("claimant") != owner:
        digest["loss"] = "misattributed"
        failures.append(
            "the journaled field_claim_lost attributes the takeover "
            f"to {losses[0][1].get('claimant')}, not the promoted "
            f"peer's claim token {owner}"
        )
    else:
        digest["loss"] = "attributed"
        digest["loss_seq"] = losses[0][0]
    walk_entries = role_walk(added)
    if any(origin is None for _from, _to, origin in walk_entries):
        raise Inconclusive(
            "the demoted peer's role walk carries no switch origin "
            "— the fenced attribution the episode's evidence reads; "
            f"the pinned release predates the contract: {walk_entries}"
        )
    if walk_entries != [
        ("active", "demoting", "fenced"),
        ("demoting", "standby", "fenced"),
    ]:
        digest["walk"] = "diverged"
        failures.append(
            "the demoted peer's journaled role walk is "
            f"{walk_entries}, expected the fenced "
            "active → demoting → standby"
        )
    else:
        digest["walk"] = walk
    peer_added = pair.get(
        f"{promoted_url}/journal", "GET /journal", failures
    )[peer_floor:]
    peer_losses = lost_entries(peer_added)
    if peer_losses:
        failures.append(
            "the promoted peer journaled a field_claim_lost it "
            f"never lost: {peer_losses}"
        )
    peer_walk = role_walk(peer_added)
    if peer_walk != [
        ("standby", "promoting", "request"),
        ("promoting", "active", "request"),
    ]:
        failures.append(
            "the promoted peer's journaled role walk is "
            f"{peer_walk}, expected the request-origin "
            "standby → promoting → active"
        )
    if failures:
        return digest

    # The bounded re-join: the demoted peer resolves the standing
    # claim's declared monitor through the verified tracking path
    # and reports `tracking` inside the window — the wedge the
    # finding names is the failure this bound catches.
    rejoin = [sync_kind(settled)]
    tracked = settled if claim_reclaim.tracking(settled) else None
    for _ in range(REJOIN_SCANS):
        if tracked is not None:
            break
        pair.scan(promoted_url, failures)
        pair.scan(demoted_url, failures)
        report = pair.get(f"{demoted_url}/role", "GET /role", failures)
        rejoin.append(sync_kind(report))
        if claim_reclaim.tracking(report):
            tracked = report
            break
    evidence[label + "_rejoin"] = rejoin
    if tracked is None:
        digest["rejoin"] = "wedged"
        failures.append(
            "the demoted peer never re-joined — the "
            f"stranded-standby wedge the finding names; its sync "
            f"readings stayed {rejoin}"
        )
        return digest
    tick_delta = None
    if isinstance(demote_tick, int) and isinstance(
        tracked.get("tick"), int
    ):
        tick_delta = tracked["tick"] - demote_tick
    evidence[label + "_rejoin_ticks"] = tick_delta
    digest["rejoin"] = "tracked"
    digest["rejoin_ticks"] = tick_delta

    # The verified-source evidence: the demoted peer resolves the
    # endpoint the standing claim declared. A fresh demotion
    # journals its tracking_source_adopted; a repeat demotion may
    # converge through the process-lifetime pin an earlier adoption
    # left — either way the durable journal must carry an adoption
    # naming the declared monitor's port. The configured-source
    # sibling owes none — its declared --standby source needs no
    # verification.
    journal = pair.get(f"{demoted_url}/journal", "GET /journal", failures)
    adoptions = adopted_entries(journal[floor:])
    evidence[label + "_adopted"] = adoptions
    sources = [record.get("source") for _seq, record in adoptions]
    journal_file = demoted_files.get("journal_file")
    durable_sources = (
        demote_reconvergence.adopted_sources(journal_file)
        if journal_file is not None
        else []
    )
    pinned = any(
        str(source).endswith(":" + promoted_port)
        for source in durable_sources
    )
    if len(adoptions) > 1:
        digest["adopted"] = "duplicated"
        failures.append(
            f"the demoted peer journaled {len(adoptions)} "
            "tracking_source_adopted records for one re-join: "
            f"{sources}"
        )
    elif len(adoptions) == 1:
        if str(sources[0]).endswith(":" + promoted_port):
            digest["adopted"] = "resolved"
            digest["adopted_seq"] = adoptions[0][0]
        else:
            digest["adopted"] = "foreign"
            failures.append(
                f"the adopted source {sources[0]} does not resolve "
                "the endpoint the standing claim declared "
                f"(:{promoted_port})"
            )
    elif adoption_owed:
        if pinned:
            digest["adopted"] = "resolved"
        else:
            digest["adopted"] = "silent"
            failures.append(
                "the demoted peer resolved no verified tracking "
                "source — neither a journaled adoption this "
                "demotion nor a durable pin naming the declared "
                f"monitor on :{promoted_port}"
            )
    else:
        digest["adopted"] = "configured"

    # The re-joined line document the demoted peer now serves — the
    # honest non-owning stamp the contract's `source_owns_field`
    # records plus the propagated `line_owner` naming the promoted
    # peer the claim declared.
    doc = pair.get(
        f"{demoted_url}/checkpoint", "GET /checkpoint", failures
    )
    evidence[label + "_checkpoint"] = doc
    if (
        "source_owns_field" not in doc
        or "line_owner" not in doc
    ):
        raise Inconclusive(
            "the re-joined peer serves a checkpoint without the "
            "field-ownership stamps — the pinned release predates "
            f"the field-arbitrated monitor contract: {doc}"
        )
    if doc.get("source_owns_field") is not False:
        digest["document"] = "foreign"
        failures.append(
            "the re-joined peer serves a checkpoint whose "
            "source_owns_field is "
            f"{doc.get('source_owns_field')} — a tracking peer's "
            "honest stamp is the serving run's non-ownership"
        )
    elif not str(doc.get("line_owner")).endswith(":" + promoted_port):
        digest["document"] = "misnamed"
        failures.append(
            f"the re-joined peer serves line_owner "
            f"{doc.get('line_owner')} — expected the promoted peer "
            f"on :{promoted_port}"
        )
    else:
        digest["document"] = "verified"

    # The durable mirror: the manifest-declared --journal-file
    # carries the same loss and — where owed — adoption records.
    if journal_file is None:
        raise Inconclusive(
            "the manifest's pair declares no journal_file on the "
            "demoted peer — the durable half of the re-join audit "
            "is absent"
        )
    kinds = durable_kinds(journal_file)
    evidence[label + "_durable"] = sorted(kinds)
    if "field_claim_lost" not in kinds:
        failures.append(
            "the durable journal file carries no "
            "field_claim_lost record"
        )
    if adoption_owed and "tracking_source_adopted" not in kinds:
        failures.append(
            "the durable journal file carries no "
            "tracking_source_adopted record — the adoption the "
            "demoted peer owes went unrecorded"
        )

    # The settled pair shape: exactly one active — the promoted
    # peer — with the demoted owner tracking it, then a short
    # tracking-first pull train proving the reconvergence holds.
    demoted_role = pair.get(
        f"{demoted_url}/role", "GET /role", failures
    )
    promoted_role = pair.get(
        f"{promoted_url}/role", "GET /role", failures
    )
    if promoted_role.get("role") != "active":
        digest["pair"] = "split"
        failures.append(
            f"the promoted peer reports "
            f"{promoted_role.get('role')!r}, expected active"
        )
    elif not claim_reclaim.tracking(demoted_role):
        digest["pair"] = "untracked"
        failures.append(
            f"the demoted peer reports {demoted_role} — expected "
            "a tracking standby"
        )
    else:
        held = []
        for _ in range(HOLD_TICKS):
            _tracked, _owner = rig.tick(
                demoted_url, promoted_url, failures
            )
            report = pair.get(
                f"{demoted_url}/role", "GET /role", failures
            )
            held.append(sync_kind(report))
        if any(kind != "tracking" for kind in held):
            digest["pair"] = "unconverged"
            failures.append(
                "the re-joined peer dropped out of tracking across "
                f"the pull train: {held}"
            )
        else:
            digest["pair"] = "settled"
            digest["held"] = held
            digest["demoted_role"] = demoted_role
            digest["promoted_role"] = promoted_role
    return digest


def monitor_less_window(
    rig, owner_url, peer_url, verdict_io, foreign_io, failures, evidence
):
    """The #1045 amendment's window: a held tool claim declaring no
    monitor demotes the field owner the same way but leaves the
    demoted peer un-converged only while the claim stands — a
    controller-owned claim re-seating the field with its declared
    monitor reconverges it. Returns the window's digest dict."""
    digest = {}
    peer_port = monitor_port(peer_url)
    floor = len(
        pair.get(f"{owner_url}/journal", "GET /journal", failures)
    )

    # The foreign tool claim: `controller: false` declares no
    # monitor — the claim the amendment names.
    claim = foreign_io.request(
        {"op": "claim_writer", "owner": TOOL_CLAIM, "controller": False}
    )
    evidence["window_claim"] = claim
    result = claim.get("result")
    if result == "claimed_shared":
        raise Abort(
            f"the tool claim answered claimed_shared — a live "
            f"attachment already holds the leg's fixed token "
            f"{TOOL_CLAIM:#x}: {claim}"
        )
    if result != "done":
        raise Inconclusive(
            f"the monitor-less tool claim answered {claim} — the "
            "consumer harness admits no claim-staging lever, or "
            "the pinned plant predates it"
        )
    digest["claim"] = "held"

    verdict = verdict_io.request({"op": "step", "dt": 0})
    evidence["window_verdict"] = verdict
    owner = claim_reclaim.verdict_owner(verdict)
    if owner is None:
        raise Inconclusive(
            "the tool claim's fencing verdict names no standing "
            f"owner: {verdict}"
        )
    if owner != TOOL_CLAIM:
        digest["verdict"] = "foreign"
        failures.append(
            f"the tool claim never stood as field owner: {verdict}"
        )
    elif "monitor" in ((verdict or {}).get("error") or {}):
        digest["verdict"] = "named"
        failures.append(
            "a tool claim that declared no monitor names one in "
            f"the fencing verdict: {verdict}"
        )
    else:
        digest["verdict"] = "monitor-less"
    if failures:
        return digest

    # The superseded owner demotes in place under the tool claim —
    # the demotion machinery owes the attributed loss journal.
    walk = []
    settled = None
    for _ in range(WATCH_SCANS):
        pair.scan(owner_url, failures)
        report = pair.get(f"{owner_url}/role", "GET /role", failures)
        walk.append(report.get("role"))
        if report.get("role") == "standby":
            settled = report
            break
    evidence["window_demotion"] = walk
    if settled is None:
        digest["demotion"] = "held"
        failures.append(
            "the superseded peer never reported standby under the "
            f"tool claim — the role walk stayed {walk}"
        )
        return digest
    digest["demotion"] = "in-place"
    journal = pair.get(f"{owner_url}/journal", "GET /journal", failures)
    losses = lost_entries(journal[floor:])
    evidence["window_losses"] = losses
    if losses and all(
        record.get("claimant") == TOOL_CLAIM for _seq, record in losses
    ):
        digest["window_loss"] = "attributed"
    elif losses and any(
        "claimant" not in record for _seq, record in losses
    ):
        raise Inconclusive(
            "the tool-claim supersede journaled a field_claim_lost "
            f"carrying no claimant — the pinned release predates "
            f"the attribution contract: {losses}"
        )
    else:
        digest["window_loss"] = "unattributed"
        failures.append(
            "the tool-claim supersede journaled no attributed "
            f"field_claim_lost: {losses}"
        )

    # The hold window: while the monitor-less tool claim stands the
    # demoted peer must NOT converge — `unsynchronized`, or the
    # keyed orphaned-tracked reading — the sibling never
    # self-promotes (unarmed), and the verdict keeps naming the
    # monitor-less owner.
    hold, parked = [], set()
    for _ in range(HOLD_SCANS):
        pair.scan(owner_url, failures)
        pair.scan(peer_url, failures)
        owner_role = pair.get(
            f"{owner_url}/role", "GET /role", failures
        )
        peer_role = pair.get(f"{peer_url}/role", "GET /role", failures)
        probe = verdict_io.request({"op": "step", "dt": 0})
        hold.append(
            {
                "owner": owner_role.get("role"),
                "owner_sync": sync_kind(owner_role),
                "peer": peer_role.get("role"),
                "verdict_owner": (
                    "tool"
                    if claim_reclaim.verdict_owner(probe) == TOOL_CLAIM
                    else "other"
                ),
            }
        )
        parked.add(sync_kind(owner_role))
    evidence["window_hold"] = hold
    if "tracking" in parked:
        digest["parked"] = "tracking"
        failures.append(
            "the demoted peer reported tracking while the field "
            "stood under a monitor-less tool claim"
        )
    elif "missing" in parked:
        digest["parked"] = "silent"
        failures.append(
            "the demoted peer stopped answering /role during the "
            "window"
        )
    elif any(row["owner"] != "standby" for row in hold):
        digest["parked"] = "promoted"
        failures.append(
            "the demoted peer left standby while a monitor-less "
            f"tool claim stood: {hold}"
        )
    else:
        digest["parked"] = "unconverged"
    if any(row["peer"] == "active" for row in hold):
        failures.append(
            "the sibling promoted itself during the monitor-less "
            f"window: {hold}"
        )
    if any(row["verdict_owner"] != "tool" for row in hold):
        failures.append(
            "the fencing verdict moved off the tool claim during "
            f"the held window: {hold}"
        )
    if failures:
        return digest

    # The re-seat: the sibling's promote lands a controller-owned
    # claim — a tool claim is never an incumbent — declaring its
    # monitor; the demoted peer then sees a declared monitor and
    # converges.
    status, report = pair.request(f"{peer_url}/promote", {})
    evidence["window_reseat"] = {"status": status, "body": report}
    if status != 200 or report.get("role") != "promoting":
        digest["reseat"] = "refused"
        failures.append(
            "the sibling promote under the tool claim answered "
            f"{status} {report}"
        )
        return digest
    digest["reseat"] = "controller"
    pair.scan(peer_url, failures)
    verdict = verdict_io.request({"op": "step", "dt": 0})
    evidence["window_reseat_verdict"] = verdict
    reseat_owner = claim_reclaim.verdict_owner(verdict)
    if reseat_owner == TOOL_CLAIM:
        digest["reseat_verdict"] = "foreign"
        failures.append(
            "the sibling's controller claim never stood as owner "
            f"after the preempt: {verdict}"
        )
    elif not str(verdict_monitor(verdict)).endswith(":" + peer_port):
        digest["reseat_verdict"] = "absent"
        failures.append(
            "the re-seated claim declares no monitor for the "
            f"re-join to resolve: {verdict}"
        )
    else:
        digest["reseat_verdict"] = "named"

    converge = []
    tracked = None
    for _ in range(REJOIN_SCANS):
        pair.scan(peer_url, failures)
        pair.scan(owner_url, failures)
        report = pair.get(f"{owner_url}/role", "GET /role", failures)
        converge.append(report.get("role") + "/" + sync_kind(report))
        if claim_reclaim.tracking(report):
            tracked = report
            break
    evidence["window_converge"] = converge
    if tracked is None:
        digest["converge"] = "wedged"
        failures.append(
            "the demoted peer stayed un-converged after a "
            "controller-owned claim re-seated the field — its "
            f"reports stayed {converge}"
        )
    else:
        digest["converge"] = "tracked"
    return digest


def stranded_rejoin_pass(args, tamper):
    """The stranded-standby re-join run: converge, gate the contract
    surface, both involuntary-demote directions, the monitor-less
    foreign-claim window, and the launch-layout restore. Returns
    `(digest_entries, evidence, failures)`; raises `Inconclusive`
    where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "stranded-rejoin leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = verdict_io = foreign_io = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        # The claim attachments the episode stages through: the
        # rig's own client stays read-only — `verdict_io` runs the
        # third-party mutation probes and never holds a claim, and
        # `foreign_io` holds the monitor-less tool claim the window
        # stages.
        verdict_io = simulate.PlantClient(rig.plant_addr)
        foreign_io = simulate.PlantClient(rig.plant_addr)

        # Phase 1 — convergence and the contract surface: the
        # launched active's recorded claim token, the standing
        # claim fencing foreign mutations and naming its owner and
        # declared monitor, the served checkpoint carrying the
        # ownership stamps, and the durable journal files the
        # manifest declares. Each absence is the release predating
        # the contract, never a violation of it.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
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
                "the claim lifecycle the stranded-standby contract "
                "rides on"
            )
        if rig.duty_files.get("journal_file") is None or (
            rig.standby_files.get("journal_file") is None
        ):
            raise Inconclusive(
                "the manifest's pair declares no journal files — "
                "the durable half of the re-join audit is absent"
            )
        probe0 = verdict_io.request({"op": "step", "dt": 0})
        evidence["probe0"] = probe0
        if not claim_reclaim.mutation_fenced(probe0):
            failures.append(
                "the field held no writer claim after convergence "
                f"— a third-party probe answered {probe0}, so the "
                "leg has no standing claim to lose"
            )
            raise Abort
        if claim_reclaim.verdict_owner(probe0) is None:
            raise Inconclusive(
                "the fencing verdict names no standing owner — the "
                "pinned release predates the verdict attribution "
                f"the contract's claimants read: {probe0}"
            )
        if claim_reclaim.verdict_owner(probe0) != owner_token:
            failures.append(
                "the standing claim names a foreign token, not the "
                f"launch owner — the pair is not in its launch "
                f"claim state: {probe0}"
            )
            raise Abort
        declared0 = verdict_monitor(probe0)
        if declared0 is None:
            raise Inconclusive(
                "the fencing verdict names no declared monitor — "
                "the pinned release predates the "
                "claim-declared-monitor contract the stranded "
                f"re-join resolves: {probe0}"
            )
        if not str(declared0).endswith(":" + monitor_port(duty_url)):
            failures.append(
                f"the standing claim's declared monitor {declared0} "
                "does not resolve the launch owner on "
                f":{monitor_port(duty_url)}"
            )
            raise Abort
        doc0 = pair.get(
            f"{duty_url}/checkpoint", "GET /checkpoint", failures
        )
        evidence["doc0"] = doc0
        if (
            "source_owns_field" not in doc0
            or "line_owner" not in doc0
        ):
            raise Inconclusive(
                "the field owner serves a checkpoint without the "
                "field-ownership stamps — the pinned release "
                "predates the field-arbitrated monitor contract: "
                f"{doc0}"
            )
        if doc0.get("source_owns_field") is not True or not str(
            doc0.get("line_owner")
        ).endswith(":" + monitor_port(duty_url)):
            failures.append(
                f"the field owner's checkpoint stamps "
                f"source_owns_field={doc0.get('source_owns_field')} "
                f"line_owner={doc0.get('line_owner')} — expected "
                f"the owner's own stamps naming "
                f":{monitor_port(duty_url)}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "gate",
                "claim": "fenced",
                "owner": "named",
                "monitor": "named",
                "stamps": "present",
            }
        )

        # Phase 2 — cycle 1: the tracking standby promotes; the
        # launch owner demotes in place and re-joins through the
        # declared monitor — the adoption it owes, carrying no
        # configured --standby source of its own.
        cycle = rejoin_cycle(
            rig,
            duty_url,
            standby_url,
            rig.duty_files,
            True,
            verdict_io,
            failures,
            evidence,
            "cycle1",
        )
        digest_entries.append({"phase": "cycle1", **cycle})
        if failures:
            raise Abort

        if tamper == "expect-wedge":
            # The doctored expectation — the leg asserts the
            # demoted peer may stay un-converged, the
            # stranded-standby wedge the contract closed. The
            # honest bounded re-join must fail it.
            failures.append(
                "the doctored expectation wanted the demoted peer "
                "stranded — the honest run re-joined tracking "
                "inside the bound"
            )
            raise Abort

        # Phase 3 — cycle 2: the launch owner promotes back through
        # the same involuntary entry; the demoted sibling re-joins
        # through its configured --standby source, no adoption
        # journal owed.
        cycle = rejoin_cycle(
            rig,
            standby_url,
            duty_url,
            rig.standby_files,
            False,
            verdict_io,
            failures,
            evidence,
            "cycle2",
        )
        digest_entries.append({"phase": "cycle2", **cycle})
        if failures:
            raise Abort

        # Phase 4 — the monitor-less window: the held tool claim,
        # the parked demoted peer, and the controller-owned
        # re-seat — the #1045 amendment.
        window = monitor_less_window(
            rig,
            duty_url,
            standby_url,
            verdict_io,
            foreign_io,
            failures,
            evidence,
        )
        digest_entries.append({"phase": "window", **window})
        if failures:
            raise Abort

        # Phase 5 — the launch roles restored: the promoted peer
        # demotes onto its configured source and the reconverged
        # peer promotes, the pair landing back on the launch
        # layout with the claim re-seated under the owner's own
        # token.
        rig.demote(standby_url, failures, "the promoted peer")
        rig.promote(
            duty_url, failures, "the demoted peer"
        )
        restored = []
        done = False
        for _ in range(RESTORE_SCANS):
            pair.scan(duty_url, failures)
            pair.scan(standby_url, failures)
            duty_role = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            peer_role = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            restored.append(
                duty_role.get("role") + "/" + peer_role.get("role")
            )
            if duty_role.get("role") == "active" and (
                claim_reclaim.tracking(peer_role)
            ):
                done = True
                break
        evidence["restore"] = restored
        if not done:
            digest_entries.append(
                {"phase": "restore", "restored": "unrestored"}
            )
            failures.append(
                "the launch layout never restored — the pair's "
                f"role reports stayed {restored}"
            )
            raise Abort
        post = verdict_io.request({"op": "step", "dt": 0})
        evidence["post_probe"] = post
        seated = "owner" if claim_reclaim.verdict_owner(
            post
        ) == owner_token else "foreign"
        if seated != "owner":
            failures.append(
                "the restored claim does not name the launch "
                f"owner's own token: {post}"
            )
            raise Abort
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        evidence["restored_at"] = duty_role.get("tick")
        digest_entries.append(
            {
                "phase": "restore",
                "watch": restored,
                "restored": "seated",
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
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--tamper",
        choices=["expect-wedge"],
        help="doctor the leg's expectation to the pre-contract "
        "shape — the pass must fail naming the stranded wedge",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = stranded_rejoin_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "stranded-rejoin: the doctored expectation wanted "
                "the demoted peer stranded — an inconclusive run "
                "offers the doctored case no evidence"
            )
            return 1
        eprint(f"stranded-rejoin: inconclusive — {inconclusive}")
        print(f"stranded-rejoin-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"stranded-rejoin: {line}")
        return 1
    for failure in failures:
        eprint(f"stranded-rejoin: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"stranded-rejoin: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"stranded-rejoin-digest {digest} — tracking by tick "
        f"{evidence['converged']}, both involuntary directions "
        f"re-joined off the declared monitor, the monitor-less "
        "window held, launch roles restored at tick "
        f"{evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
