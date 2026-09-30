#!/usr/bin/env python3
"""The own-tick-ahead rejoin leg for the reference plant — the
consumer-side proof that on the manifest-declared *unkeyed* pair a
demoted or fenced ex-owner re-joins the line through the standing
claim's declared monitor regardless of how far the promoted
successor's own run tick leads the prober's — the field-arbitrated
claimed-monitor adoption verifying each document's declared stream
position, never the probing run's own paced tick (WW-ENG-003,
WW-LCM-001 — the #1269 ahead-bound contract the qa rig's
`a_demoted_ex_owner_rejoins_a_successor_carrying_the_outage_lead`
pins, mirrored at the customer boundary).

The stranded-rejoin leg proves the involuntary promote re-joins a
demoted peer whose run ticks sat beside the line's; the
foreign-claim leg proves the monitor-less window and the released
field's two resolutions on the same unkeyed deployment the manifest
declares. This leg pins the finding's own trigger on exactly that
shape: the routine UI-driven `POST /promote` on a tracking standby
whose paced run clock accrues a permanent lead over the line's
stream position through every source freeze it survives — a lead
the promotion carries — so the promoted successor's served run
tick leads the ex-owner's own past MAX_ANNOUNCED_AHEAD while its
declared stream position honestly locates the line. On the
defective build the demoted peer's verify compared the successor's
run tick against the prober's own — own ticks are not synchronized
to the line — and refused the field's own answer `Ahead` forever,
stranding the ex-owner `standby`/`unsynchronized` with a silent
journal. The run:

- launches the pair unkeyed on its declared wildcard binds — the
  rig's `pair.launch_pair` keys the pair for the announced-source
  contracts, so this leg composes the same unkeyed spawn sequence
  the foreign-claim leg's `launch_unkeyed` shares — converges to
  `active`/`tracking`, and gates the contract surface: the recorded
  claim token, the fenced verdict naming the owner and its declared
  dialable monitor, the checkpoint's ownership stamps, the declared
  journal files — each absence the pre-contract shape, reported
  `ahead-bound-rejoin-digest inconclusive`, never a failure;
- stages the successor's accumulated tick lead through the
  driven-scan lever: the tracking peer's `POST /scan` ticks its
  paced clock while the field owner is left un-scanned — its served
  stream standing still — until the tracker's own run tick leads
  the line past MAX_ANNOUNCED_AHEAD, its checkpoint declaring the
  `stream_tick` the contract's skew bound is written in;
- runs the routine promote — `POST /promote` on the lead-carrying
  standby with no `POST /demote` on the owner first — so the
  successor's standing claim fences and demotes the field owner in
  place;
- asserts through the demoted peer's serving monitor and its
  manifest-declared durable journal that it re-joins through the
  recorded claimed-monitor adoption inside the documented bound —
  one `tracking_source_adopted` naming the successor's declared
  endpoint beside the attributed `field_claim_lost` and the fenced
  demote walk — converging `tracking` rather than stranding
  `unsynchronized` past the bound, no skew-bound source refusal
  journaled;
- proves a later promote answers the converged path: the
  documented switch re-seats the launch owner — the reconverged
  ex-owner's promote answering `promoting` — restoring the pair's
  launch roles and claim state.

The contract postdates the pinned release line — every released
artifact set predates it until #1269's fix lands in a release.
Where the launched tooling predates it — no recorded owner-token
claim line, a fencing verdict naming no owner or no declared
monitor, a checkpoint without the ownership stamps, a staging that
accrues no run-tick lead, a lead-carrying checkpoint declaring no
`stream_tick`, a journaled loss carrying no claimant or a role walk
carrying no origin — the run's own evidence is the pre-contract
shape and the leg reports `ahead-bound-rejoin-digest inconclusive`
rather than asserting until the manifest repins a release carrying
the contract.

Usage:

    own_tick_ahead_rejoin.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `ahead-bound-rejoin-digest <sha256>` line prints —
the check runs two passes and compares them
(`ahead-bound-rejoin-nondeterministic`). A contract violation
reports `ahead-bound-rejoin: …` lines on stderr and exits 1 — the
check's `ahead-bound-rejoin-failed`. `--tamper expect-wedge`
doctors the leg's own expectation to the pre-contract shape —
asserting the demoted peer may stay stranded `unsynchronized`, the
wedge the finding names — so the leg proves its re-join assertion
fires on the honest reconvergence rather than passing an
unexercised contract.
"""

import argparse
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import claim_reclaim
import failover
import foreign_claim_release
import pair
import simulate
import stranded_rejoin


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored case.
# The doctored case: a leg asserting the demoted peer may stay
# stranded `unsynchronized` — the ahead-bound wedge the contract
# closed — must surface the named diagnostic on the honest re-join
# rather than passing an unexercised contract.
LEG = {
    "order": 680,
    "title": "the own-tick-ahead rejoin leg",
    "passes": "ahead-bound-rejoin-leg",
    "failed": "ahead-bound-rejoin-failed",
    "tampers": [
        {
            "name": "expect-wedge",
            "passed": "an expect-wedge case passed the own-tick-ahead-rejoin leg",
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
    product failure. The first arg is the stable reason the
    `inconclusive` digest line prints — two identical passes must
    share it — and the optional second arg the run's own evidence,
    reported on stderr only, where ephemeral verdicts, endpoints,
    and minted tokens belong."""


# The contract's skew bound and the driven-scan windows each phase
# runs: the staged lead — the tracking peer's paced scans while the
# owner's served stream stands still — must clear MAX_ANNOUNCED_AHEAD
# the way the rig's forty-tick outage does; the fenced demote-in-place
# settles inside a couple of scans; the claimed-monitor rejoin is the
# bounded window the contract names — the finding's indefinite
# `unsynchronized` wedge is the failure the bound catches; and the
# restored pair's pull train proves the reconvergence holds.
MAX_ANNOUNCED_AHEAD = 32
LEAD_SCANS = 40
WATCH_SCANS = 6
REJOIN_SCANS = 12
HOLD_TICKS = 3


def refused_entries(entries):
    """The `tracking_source_refused` records of a journal entry list
    — `(seq, record)` pairs in journal order: the defective build's
    durable signature, a refused verify journaling once per
    (source, reason) signature where the rejoin owed an adoption."""
    return [
        (entry["seq"], entry["event"]["tracking_source_refused"])
        for entry in entries
        if "tracking_source_refused" in entry.get("event", {})
    ]


def monitor_port(url):
    """The served monitor's port — the endpoint suffix a declared
    monitor, an adopted source, or a propagated `line_owner` must
    resolve to."""
    return stranded_rejoin.monitor_port(url)


def monitor_addr(url):
    """The peer's dialable monitor address as `host:port`."""
    return foreign_claim_release.monitor_addr(url)


def own_tick_ahead_rejoin_pass(args, tamper):
    """The own-tick-ahead rejoin run: launch the unkeyed declared
    pair, converge, gate the contract surface, stage the
    successor's ahead-bound lead through driven scans, run the
    routine promote, and assert the fenced ex-owner's
    claimed-monitor re-join inside the bound — journaled,
    converged, promotable, launch roles restored. Returns
    `(digest_entries, evidence, failures)`; raises `Inconclusive`
    where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "own-tick-ahead rejoin leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    rig = verdict_io = None
    try:
        rig = foreign_claim_release.launch_unkeyed(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        # The third-party claim attachment the fencing probes run
        # through — read-only, never holding a claim of its own.
        verdict_io = simulate.PlantClient(rig.plant_addr)
        duty_port = monitor_port(duty_url)
        standby_port = monitor_port(standby_url)

        # Phase 1 — the declared wildcard binds deployed verbatim:
        # the unkeyed corner's own deployment shape, where a claim
        # declaring its bind address is the undialable declaration
        # the arbitration's normalization must substitute.
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
        # launched active's recorded claim token, the standing
        # claim fencing third-party mutations while naming its
        # owner and its declared dialable monitor, the served
        # checkpoint carrying the ownership stamps, and the
        # manifest-declared durable journal files. Each absence is
        # the release predating the contract, never a violation.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "binds": binds,
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"].get("role"),
                "standby_sync": stranded_rejoin.sync_kind(
                    converged["standby_role"]
                ),
            }
        )
        owner_token = failover.owner_token(rig.duty_preamble)
        if owner_token is None:
            raise Inconclusive(
                "the launched active recorded no claim line — the "
                "pinned release claims only on promotion, predating "
                "the claim lifecycle the rejoin contract rides on",
                f"the duty preamble was {rig.duty_preamble}",
            )
        if rig.duty_files.get("journal_file") is None or (
            rig.standby_files.get("journal_file") is None
        ):
            raise Inconclusive(
                "the manifest's pair declares no journal files — "
                "the durable half of the rejoin audit is absent"
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
                "the contract's claimants read",
                f"the standing fencing verdict was {probe0}",
            )
        if claim_reclaim.verdict_owner(probe0) != owner_token:
            failures.append(
                "the standing claim names a foreign token, not the "
                f"launch owner — the pair is not in its launch "
                f"claim state: {probe0}"
            )
            raise Abort
        declared0 = stranded_rejoin.verdict_monitor(probe0)
        if declared0 is None:
            raise Inconclusive(
                "the standing claim declares no monitor — the "
                "pinned release predates the claim-declared "
                "monitor the claimed rejoin resolves",
                f"the standing fencing verdict was {probe0}",
            )
        kind0 = foreign_claim_release.monitor_kind(
            declared0, monitor_addr(duty_url)
        )
        if kind0 == "wildcard":
            raise Inconclusive(
                "the standing claim declares its wildcard bind "
                "verbatim — the pinned release predates the "
                "claim-monitor normalization",
                f"the declared monitor was {declared0}",
            )
        if kind0 != "owner":
            failures.append(
                f"the standing claim declares monitor {declared0} "
                "— not the field owner's dialable monitor "
                f"{monitor_addr(duty_url)}"
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
                "predates the field-arbitrated monitor contract",
                f"the checkpoint serves {sorted(doc0)}",
            )
        if doc0.get("source_owns_field") is not True or not str(
            doc0.get("line_owner")
        ).endswith(":" + duty_port):
            failures.append(
                f"the field owner's checkpoint stamps "
                f"source_owns_field={doc0.get('source_owns_field')} "
                f"line_owner={doc0.get('line_owner')} — expected "
                f"the owner's own stamps naming :{duty_port}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "gate",
                "probe": "fenced",
                "owner": "named",
                "monitor": "owner",
                "stamps": "present",
            }
        )

        # Phase 3 — the staged lead: the tracking peer's driven
        # scans pace its run clock while the field owner, left
        # un-scanned, serves its stream standing still — the
        # reproduction's frozen source. Each pull applies the same
        # frozen document onto a later run tick, so the tracker
        # accrues the permanent lead the promotion carries and its
        # checkpoint declares the stream position the contract's
        # skew bound is written in.
        frozen = converged["ticks"][-1]
        for _ in range(LEAD_SCANS):
            pair.scan(standby_url, failures)
        staged_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        staged_sync = stranded_rejoin.sync_kind(staged_role)
        evidence["staged_sync"] = staged_sync
        if staged_sync == "missing":
            raise Inconclusive(
                "the staged tracker's role report carries no sync "
                "vocabulary — the pinned release predates the "
                "contract's verdicts",
                f"GET /role answered {staged_role}",
            )
        if staged_sync != "tracking":
            failures.append(
                f"the staged tracker reported "
                f"{staged_role.get('role')}/{staged_sync} — the "
                "routine promote rides on a tracking standby"
            )
            raise Abort
        successor = pair.get(
            f"{standby_url}/checkpoint", "GET /checkpoint", failures
        )
        own_doc = pair.get(
            f"{duty_url}/checkpoint", "GET /checkpoint", failures
        )
        evidence["successor"] = successor
        own_tick = own_doc.get("tick")
        succ_tick = successor.get("tick")
        if not isinstance(succ_tick, int) or not isinstance(
            own_tick, int
        ):
            raise Inconclusive(
                "the served checkpoints carry no integer tick — "
                "the pinned release predates the tick-domain "
                "contract",
                f"successor {successor} vs ex-owner {own_doc}",
            )
        lead = succ_tick - own_tick
        if lead <= MAX_ANNOUNCED_AHEAD:
            raise Inconclusive(
                "the driven-scan staging accrued no ahead-bound "
                "lead — the pinned release predates the "
                "never-rewind tick-domain contract the lead mints "
                "on",
                f"the successor tick {succ_tick} leads the frozen "
                f"line's {own_tick} by {lead}",
            )
        if "stream_tick" not in successor:
            raise Inconclusive(
                "the lead-carrying checkpoint declares no stream "
                "position — the pinned release predates the "
                "declared stream-lead contract the rejoin "
                "verifies on",
                f"the checkpoint serves {sorted(successor)}",
            )
        stream = successor.get("stream_tick")
        if (
            not isinstance(stream, int)
            or stream > own_tick + MAX_ANNOUNCED_AHEAD
        ):
            failures.append(
                f"the staged successor declares stream position "
                f"{stream} past the bound on the line's frozen "
                f"tick {own_tick} — a declared position leading "
                "the line past MAX_ANNOUNCED_AHEAD is the "
                "forgery shape the bound honestly refuses, not "
                "the accrued lead the contract carries"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "lead",
                "scans": LEAD_SCANS,
                "own": own_tick,
                "tick": succ_tick,
                "stream": stream,
            }
        )

        # Phase 4 — the routine promote: `POST /promote` on the
        # lead-carrying standby with no `POST /demote` on the owner
        # first — the finding's involuntary entry. The promoted
        # peer's first scan lands its claim; the superseded owner's
        # own fenced write demotes it in place, its monitor serving
        # throughout.
        floor = len(
            pair.get(f"{duty_url}/journal", "GET /journal", failures)
        )
        peer_floor = len(
            pair.get(
                f"{standby_url}/journal", "GET /journal", failures
            )
        )
        status, report = pair.request(f"{standby_url}/promote", {})
        evidence["promote"] = {"status": status, "body": report}
        if status != 200 or report.get("role") != "promoting":
            failures.append(
                "POST /promote on the lead-carrying standby "
                f"answered {status} {report} — the routine entry "
                "must answer a promoting report"
            )
            raise Abort
        walk = []
        settled = None
        for _ in range(WATCH_SCANS):
            pair.scan(standby_url, failures)
            pair.scan(duty_url, failures)
            report = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            walk.append(report.get("role"))
            if report.get("role") == "standby":
                settled = report
                break
        evidence["demotion"] = walk
        if settled is None:
            failures.append(
                "the superseded owner never demoted in place — "
                "the promoted successor took the claim but the "
                f"role walk stayed {walk}"
            )
            raise Abort
        if any(
            role not in ("active", "demoting", "standby")
            for role in walk
        ):
            failures.append(
                f"the superseded owner reported an unexpected role "
                f"walk {walk} — the demote-in-place walks only "
                "active → demoting → standby"
            )
        demote_tick = settled.get("tick")
        promoted_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if promoted_role.get("role") != "active":
            failures.append(
                f"the promoted successor reports "
                f"{promoted_role.get('role')!r}, expected active"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "promote",
                "promote": "granted",
                "demotion": "in-place",
                "walk": walk,
            }
        )
        if failures:
            raise Abort

        # The standing claim's declared monitor — the
        # field-arbitrated rendezvous the re-join must resolve —
        # on a fresh third-party probe.
        probe = verdict_io.request({"op": "step", "dt": 0})
        evidence["verdict"] = probe
        owner = claim_reclaim.verdict_owner(probe)
        declared_monitor = stranded_rejoin.verdict_monitor(probe)
        if not claim_reclaim.mutation_fenced(probe):
            failures.append(
                "the promoted successor's claim does not fence "
                f"third-party mutations: {probe}"
            )
            raise Abort
        if owner is None:
            raise Inconclusive(
                "the post-promotion fencing verdict names no "
                "standing owner — the pinned release predates the "
                "verdict attribution the loss's claimant reads",
                f"the fencing verdict was {probe}",
            )
        if declared_monitor is None:
            failures.append(
                "the promoted successor's claim declares no "
                "monitor — the field-arbitrated tracking "
                f"candidate is missing: {probe}"
            )
            raise Abort
        kind = foreign_claim_release.monitor_kind(
            declared_monitor, monitor_addr(standby_url)
        )
        if kind == "wildcard":
            raise Inconclusive(
                "the promoted claim declares its wildcard bind "
                "verbatim — the pinned release predates the "
                "claim-monitor normalization the rejoin needs",
                f"the declared monitor was {declared_monitor}",
            )
        if kind != "owner":
            failures.append(
                f"the promoted claim declares monitor "
                f"{declared_monitor} — not the successor's "
                f"dialable monitor {monitor_addr(standby_url)}"
            )
            raise Abort

        # The journaled loss the demotion owes — exactly one
        # field_claim_lost attributed to the successor's claim —
        # beside the fenced demote walk; the successor's journal
        # carries the request-origin promote walk and no loss.
        journal = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        added = journal[floor:]
        losses = stranded_rejoin.lost_entries(added)
        evidence["losses"] = losses
        if not losses:
            failures.append(
                "the demoted peer journaled no field_claim_lost "
                "during the supersede"
            )
        elif len(losses) != 1:
            failures.append(
                f"the demoted peer journaled {len(losses)} "
                "field_claim_lost records for one supersede — one "
                "record per held claim, not one per fenced write"
            )
        elif "claimant" not in losses[0][1]:
            raise Inconclusive(
                "the journaled field_claim_lost carries no "
                "claimant — the loss attribution the episode's "
                "evidence reads; the pinned release predates the "
                "contract",
                f"the journaled losses were {losses}",
            )
        elif losses[0][1].get("claimant") != owner:
            failures.append(
                "the journaled field_claim_lost attributes the "
                f"takeover to {losses[0][1].get('claimant')}, not "
                f"the promoted successor's claim token {owner}"
            )
        walk_entries = stranded_rejoin.role_walk(added)
        if any(
            origin is None for _from, _to, origin in walk_entries
        ):
            raise Inconclusive(
                "the demoted peer's role walk carries no switch "
                "origin — the fenced attribution the episode's "
                "evidence reads; the pinned release predates the "
                "contract",
                f"the role walk was {walk_entries}",
            )
        if walk_entries != [
            ("active", "demoting", "fenced"),
            ("demoting", "standby", "fenced"),
        ]:
            failures.append(
                "the demoted peer's journaled role walk is "
                f"{walk_entries}, expected the fenced "
                "active → demoting → standby"
            )
        peer_added = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )[peer_floor:]
        peer_losses = stranded_rejoin.lost_entries(peer_added)
        if peer_losses:
            failures.append(
                "the promoted successor journaled a "
                f"field_claim_lost it never lost: {peer_losses}"
            )
        peer_walk = stranded_rejoin.role_walk(peer_added)
        if peer_walk != [
            ("standby", "promoting", "request"),
            ("promoting", "active", "request"),
        ]:
            failures.append(
                "the promoted successor's journaled role walk is "
                f"{peer_walk}, expected the request-origin "
                "standby → promoting → active"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "loss",
                "loss": "attributed",
                "walk": "fenced",
                "peer_walk": "request",
            }
        )

        # Phase 5 — the bounded re-join through the claimed-monitor
        # rendezvous: the demoted peer owns no configured source
        # and no proven hint the unkeyed run could verify, so the
        # standing claim's declared monitor is the only candidate
        # its re-join resolves — the verify reading the successor's
        # declared stream position, never the prober's own tick.
        # The finding's indefinite wedge is the failure this bound
        # catches.
        rejoin = [stranded_rejoin.sync_kind(settled)]
        tracked = settled if claim_reclaim.tracking(settled) else None
        for _ in range(REJOIN_SCANS):
            if tracked is not None:
                break
            pair.scan(standby_url, failures)
            pair.scan(duty_url, failures)
            report = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            rejoin.append(stranded_rejoin.sync_kind(report))
            if claim_reclaim.tracking(report):
                tracked = report
                break
        evidence["rejoin"] = rejoin
        if tracked is None:
            digest_entries.append(
                {"phase": "rejoin", "rejoin": "wedged", "watch": rejoin}
            )
            failures.append(
                "the demoted ex-owner never re-joined — the "
                "ahead-bound strand the contract closed; its sync "
                f"readings stayed {rejoin}"
            )
            raise Abort
        tick_delta = None
        if isinstance(demote_tick, int) and isinstance(
            tracked.get("tick"), int
        ):
            tick_delta = tracked["tick"] - demote_tick
        evidence["rejoin_ticks"] = tick_delta
        digest_entries.append(
            {
                "phase": "rejoin",
                "rejoin": "tracked",
                "rejoin_ticks": tick_delta,
            }
        )

        # The recorded claimed-monitor adoption: one
        # tracking_source_adopted naming the successor's declared
        # endpoint above the episode floor — and no skew-bound
        # source refusal, the defective build's durable signature,
        # beside it.
        journal = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        adoptions = stranded_rejoin.adopted_entries(journal[floor:])
        evidence["adopted"] = adoptions
        if len(adoptions) != 1:
            failures.append(
                f"the demoted peer journaled {len(adoptions)} "
                "tracking_source_adopted records for one re-join "
                "— the claimed-monitor rendezvous must journal "
                "once"
            )
        elif not str(adoptions[0][1].get("source")).endswith(
            ":" + standby_port
        ):
            failures.append(
                f"the adopted source "
                f"{adoptions[0][1].get('source')} does not resolve "
                "the endpoint the standing claim declared "
                f"(:{standby_port})"
            )
        refusals = refused_entries(journal[floor:])
        evidence["refusals"] = refusals
        skew = [
            record
            for _seq, record in refusals
            if "skew bound" in str(record.get("detail"))
        ]
        if skew:
            failures.append(
                "the demoted peer journaled a skew-bound source "
                f"refusal — the defective build's own-tick "
                f"comparison, durable evidence of the strand: "
                f"{skew}"
            )
        journal_file = rig.duty_files.get("journal_file")
        kinds = stranded_rejoin.durable_kinds(journal_file)
        evidence["durable"] = sorted(kinds)
        for owed in ("field_claim_lost", "tracking_source_adopted"):
            if owed not in kinds:
                failures.append(
                    f"the demoted peer's durable journal file "
                    f"carries no {owed} record — the journaled "
                    "evidence the rejoin owes"
                )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "adopted",
                "adopted": "resolved",
                "durable": "recorded",
            }
        )

        # The doctored negative: a leg asserting the demoted peer
        # may stay stranded `unsynchronized` — the wedge the
        # finding names — must report the named diagnostic on the
        # honest re-join rather than passing unexercised.
        if tamper == "expect-wedge":
            failures.append(
                "the doctored expectation wanted the demoted peer "
                "stranded — the honest run re-joined tracking "
                "inside the bound"
            )
            raise Abort

        # The re-joined line document: the honest non-owning stamp
        # and the propagated line_owner naming the successor the
        # standing claim declared.
        doc = pair.get(
            f"{duty_url}/checkpoint", "GET /checkpoint", failures
        )
        evidence["document"] = doc
        if (
            "source_owns_field" not in doc
            or "line_owner" not in doc
        ):
            raise Inconclusive(
                "the re-joined peer serves a checkpoint without "
                "the field-ownership stamps — the pinned release "
                "predates the field-arbitrated monitor contract",
                f"the checkpoint serves {sorted(doc)}",
            )
        if doc.get("source_owns_field") is not False:
            failures.append(
                "the re-joined peer serves a checkpoint whose "
                f"source_owns_field is "
                f"{doc.get('source_owns_field')} — a tracking "
                "peer's honest stamp is the serving run's "
                "non-ownership"
            )
        elif not str(doc.get("line_owner")).endswith(
            ":" + standby_port
        ):
            failures.append(
                f"the re-joined peer serves line_owner "
                f"{doc.get('line_owner')} — expected the promoted "
                f"successor on :{standby_port}"
            )
        digest_entries.append(
            {"phase": "document", "document": "verified"}
        )

        # The settled pair shape: exactly one active — the
        # successor — with the demoted ex-owner tracking it, the
        # tracking-first pull train proving the reconvergence
        # holds.
        held = []
        for _ in range(HOLD_TICKS):
            _tracked, _owner = rig.tick(
                duty_url,
                standby_url,
                failures,
                diverged="the re-joined peer's image diverged from "
                "the successor's at tick {tick} — the rejoin was "
                "not bumpless",
            )
            report = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            held.append(stranded_rejoin.sync_kind(report))
        if any(kind != "tracking" for kind in held):
            failures.append(
                "the re-joined peer dropped out of tracking "
                f"across the pull train: {held}"
            )
            raise Abort
        digest_entries.append(
            {"phase": "settle", "held": held}
        )

        # Phase 6 — the later promote answering the converged
        # path: the documented switch re-seats the launch owner —
        # the reconverged ex-owner's promote answering `promoting`
        # — and restores the pair's launch roles, the demoted
        # successor tracking it through its configured source.
        restored = rig.switch(
            standby_url,
            duty_url,
            failures,
            demote_what="the lead-carrying successor",
            promote_what="the converged ex-owner",
        )
        post = verdict_io.request({"op": "step", "dt": 0})
        evidence["post_probe"] = post
        if claim_reclaim.verdict_owner(post) != owner_token:
            failures.append(
                "the restored claim does not name the launch "
                f"owner's own token: {post}"
            )
            raise Abort
        duty_role = pair.get(
            f"{duty_url}/role", "GET /role", failures
        )
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active" or (
            not claim_reclaim.tracking(standby_role)
        ):
            failures.append(
                "the pair did not restore its launch roles — "
                f"{duty_role} / {standby_role}"
            )
            raise Abort
        evidence["restored_at"] = restored["ticks"][-1]
        digest_entries.append(
            {
                "phase": "restore",
                "ticks": restored["ticks"],
                "duty_role": "active",
                "standby_role": "tracking",
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if verdict_io is not None:
            try:
                verdict_io.request({"op": "release_writer"})
            except Exception:
                pass
            verdict_io.close()
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
        "shape — asserting the demoted peer may stay stranded "
        "unsynchronized, so the pass must fail naming the wedge",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = (
            own_tick_ahead_rejoin_pass(args, args.tamper)
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "ahead-bound-rejoin: the doctored expectation "
                "wanted the demoted peer stranded — an "
                "inconclusive run offers the doctored case no "
                "evidence"
            )
            return 1
        # The digest line carries only the stable reason — the
        # run's own verdicts and endpoints report on stderr, where
        # two identical passes need not share them.
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"ahead-bound-rejoin: inconclusive — {detail}")
        print(f"ahead-bound-rejoin-digest inconclusive — {reason}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"ahead-bound-rejoin: {line}")
        return 1
    for failure in failures:
        eprint(f"ahead-bound-rejoin: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"ahead-bound-rejoin: the {args.tamper} case "
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
        f"ahead-bound-rejoin-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the staged lead carried the "
        "successor's promote past the ahead bound and the demoted "
        "ex-owner re-joined through the claimed monitor inside "
        "the bound, launch roles restored at tick "
        f"{evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
