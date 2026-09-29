#!/usr/bin/env python3
"""The holderless-claim-recovery leg for the reference plant — the
consumer-boundary mirror of the rig's
`the_fencing_loss_reclaim_preempts_the_orphan_placeholder` scenario
(WW-ENG-003, WW-LCM-001 — the #1255 contract pinned on the
manifest-declared unkeyed pair, the shipped shape the
`dual-exowner-holderless-rearm-starves-reclaim` finding reproduced
on): two successive ex-owners must not wedge the pair ownerless. A
holderless standing claim protects no live attachment, so the
fencing-loss peer's bound reclaim must preempt it; once the
preemptor's claim frees, exactly one peer returns to field-owning
`active` with no operator `promote`, and the pair reconverges to one
active plus one tracking standby.

The claim-reclaim leg (`ci/legs/claim_reclaim.py`) proves the
released-preemption lifecycle — the preemptor's hand-back leaves the
field unclaimed and the marked ex-owner's bound re-grant re-seats it.
The foreign-claim-release leg proves the monitor-less release
resolutions on this deployment shape. This leg pins the remaining
wedge the finding records: the *holderless* shape, where the claim
still stands — fencing every mutation, naming a stale owner — with
no live attachment behind it. On the defect grant (the bound ensure,
refusing any different-owner claim whether held or not) that claim
refused the reclaim on every scan and the pair lost all field
ownership until an operator promoted; the `reclaim_writer` grant
refuses only a different-owner claim with *live* holders.

With the manifest-declared pair launched unkeyed on its declared
wildcard binds and converged `active`/`tracking`, the driven
scenario stages the finding's two-ex-owner sequence:

- the involuntary transfer — `POST /promote` on the tracking
  standby while the active still owns — demotes the launch owner in
  place through its own fenced write; it re-joins the promoted
  peer's claim-declared monitor and reports `tracking` — the first
  ex-owner, its loss mark cleared by the tracked ownership;
- a dedicated plant-socket attachment's `claim_writer` — a
  monitor-less tool claim, `controller: false` — preempts the
  promoted owner: its first fenced write demotes it in place with a
  `field_claim_lost` attributed to the foreign token — the second
  ex-owner, the loss mark the reclaim probes on;
- the held window: while the foreign claim keeps a live holder,
  both ex-owners go `orphaned` on their pulls and their bound
  reclaims refuse every scan — the never-preempts-a-live-holder
  half of the grant's rule — the field recording no write and the
  shipped `dcs-plant-ctl` answering the census while its mutation
  meets the named fencing refusal;
- the hand-back the issue names: the same attachment's
  `release_writer` with `keep_claim` leaves the claim standing
  holderless under the stale token — the field unclaimed beneath a
  claim that still fences, the dead-owner/orphan-placeholder shape
  the finding wedged on;
- the recovery: driven scans on the marked ex-owner — and only
  those — land its bound reclaim on the holderless claim inside the
  bounded window, the `standby → promoting → active` walk journaled
  under the `reclaim` origin with no operator call; the other
  ex-owner pulls the re-seated owner and returns to `tracking`, the
  pair reconverging to exactly one field-owning `active` plus one
  `tracking` standby with identical images;
- the launch layout restored: the documented switch seats the
  launch owner back on the field and both manifest-declared durable
  journal files carry the episode — the attributed losses, the
  orphan and observed-claim records, and every role walk's origin.

The `claim_writer`/`release_writer` staging rides the dedicated
attachment on the same plant-socket protocol the shipped
`dcs-plant-ctl` speaks — the claim lifecycle is a driver surface the
tool's verb list deliberately does not spell — while the tool
itself drives the third-party fencing probes (`list` answering the
unfenced census, `step` refused with the named fencing detail).

The contract postdates the pinned release line: where the launched
tooling predates it — no owner-token claim line, an unanswered claim
or probe verb, a `keep_claim` release the plant does not honor, a
fencing verdict naming no owner or no declared monitor, a served
checkpoint without the ownership stamps, a standby report carrying
no sync vocabulary, a loss or orphan record unjournaled, or a role
walk without switch origins — the run's own evidence is the
pre-contract shape and the leg reports
`holderless-claim-recovery-digest inconclusive` rather than
asserting until the manifest repins a release carrying the
contract.

Usage:

    holderless_claim_recovery.py --plant-server PATH --controller PATH \
        --plant-ctl PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `holderless-claim-recovery-digest <sha256>` line
prints — the check runs two passes and compares them
(`holderless-claim-recovery-nondeterministic`). A contract violation
reports `holderless-claim-recovery: …` lines on stderr and exits 1 —
the check's `holderless-reclaim-failed`. `--tamper phantom-release`
doctors the hand-back: the release lands on an attachment that holds
nothing while the claim keeps its live holder, so the pair stays
ownerless past the release the leg believes ran — the leg must
report the named diagnostic rather than passing an unexercised
contract.
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
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a release that lands on an attachment holding
# nothing while the claim keeps its live holder — the pair stranded
# ownerless past the release the leg believes ran — must surface the
# named diagnostic rather than passing an unexercised contract.
LEG = {
    "order": 670,
    "title": "the holderless-claim-recovery leg",
    "passes": "holderless-reclaim",
    "failed": "holderless-reclaim-failed",
    "tools": {
        "plant-ctl": "dcs-plant-ctl",
    },
    "tampers": [
        {
            "name": "phantom-release",
            "passed": "a phantom-release case passed the holderless-claim-recovery leg",
            "missed": "the phantom-release case did not report its named diagnostic",
            "evidence": ["the pair stayed ownerless"],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The inconclusive classification the sibling legs raise — this leg
# stages its claim ops through `foreign_claim_release`'s helpers and
# drives `stranded_rejoin`'s re-join cycle, so both classes surface
# through the pass. The first arg is the stable reason the
# `inconclusive` digest line prints — two identical passes must share
# it — and the optional second arg the run's own evidence, reported
# on stderr only.
Inconclusive = foreign_claim_release.Inconclusive


# The driven-scan bounds the episode's phases run: the fenced demote
# settles inside a couple of scans, the claim-standing window spans a
# handful of driven scans, and the reclaim's landing — the bounded
# window the contract names, the defect's indefinite refusal being
# the failure the bound catches — mirrors the rig's scan bound.
WATCH_SCANS = 6
HOLD_SCANS = 3
RESOLVE_SCANS = 8
REJOIN_SCANS = 8

# The dedicated attachment's foreign owner token — the monitor-less
# tool claim the second demotion stages. Shared with the
# foreign-claim-release leg's constant because the fenced-demotion
# audit reads it; a fresh plant per leg means the value never
# collides.
FOREIGN_CLAIM = foreign_claim_release.FOREIGN_CLAIM

# The probe attachment's own token — used only by the contract gate's
# `reclaim_writer` ask, which must refuse the live claim it names no
# share of.
PROBE_CLAIM = 0xF0DA


def plant_ctl(tool, addr, *argv):
    """One shipped `dcs-plant-ctl` invocation — the sibling leg's
    helper."""
    return foreign_claim_release.plant_ctl(tool, addr, *argv)


def monitor_addr(url):
    """The peer's dialable monitor address as `host:port`."""
    return foreign_claim_release.monitor_addr(url)


def monitor_kind(declared, owner_addr):
    """Classify a claim-declared monitor — the sibling leg's
    `wildcard`/`owner`/`foreign` vocabulary."""
    return foreign_claim_release.monitor_kind(declared, owner_addr)


def gate_contract_surface(
    rig,
    verdict_io,
    probe_io,
    a_token,
    a_url,
    failures,
    evidence,
):
    """The contract surface the episode asserts on: the standing
    claim fencing third-party mutations while naming the launch
    owner and its declared dialable monitor, the `reclaim_writer`
    and `probe_writer` verbs answered (the gate's probe reclaim
    refusing the live claim it shares no owner with), the served
    checkpoint's ownership stamps, and the durable journal files
    the manifest declares. Each absence is the release predating
    the contract, never a violation of it."""
    if rig.duty_files.get("journal_file") is None or (
        rig.standby_files.get("journal_file") is None
    ):
        raise Inconclusive(
            "the manifest's pair declares no journal files — the "
            "durable half of the recovery audit is absent"
        )
    probe0 = verdict_io.request({"op": "step", "dt": 0})
    evidence["probe0"] = probe0
    if not claim_reclaim.mutation_fenced(probe0):
        failures.append(
            "the field held no writer claim after convergence — a "
            f"third-party probe answered {probe0}, so the leg has "
            "no standing claim to fence through"
        )
        raise Abort
    if claim_reclaim.verdict_owner(probe0) is None:
        raise Inconclusive(
            "the fencing verdict names no standing owner — the "
            "pinned release predates the verdict attribution the "
            "contract's claimants read",
            f"the standing fencing verdict was {probe0}",
        )
    if claim_reclaim.verdict_owner(probe0) != a_token:
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
            "recovery's re-join resolves",
            f"the standing fencing verdict was {probe0}",
        )
    kind0 = monitor_kind(declared0, monitor_addr(a_url))
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
            f"{monitor_addr(a_url)}"
        )
        raise Abort
    doc = pair.get(f"{a_url}/checkpoint", "GET /checkpoint", failures)
    evidence["doc0"] = doc
    if "source_owns_field" not in doc or "line_owner" not in doc:
        raise Inconclusive(
            "the served checkpoint carries no field-ownership "
            "stamps — the pinned release predates the "
            "field-arbitrated monitor contract",
            f"the checkpoint serves {sorted(doc)}",
        )
    # The reclaim verb: a different-owner probe ask must refuse the
    # launch owner's live claim — the never-preempts-a-live-holder
    # half of the grant's rule, exercised without mutating. A build
    # predating the verb answers invalid_request; a grant would be
    # the contract itself broken — a foreign reclaim must never
    # take a live claim.
    gate = probe_io.request({"op": "reclaim_writer", "owner": PROBE_CLAIM})
    evidence["reclaim_gate"] = gate
    if claim_reclaim.unsupported_verb(gate):
        raise Inconclusive(
            "reclaim_writer answered invalid_request — the pinned "
            "release predates the bound re-grant verb the "
            "holderless recovery probes on",
            f"reclaim_writer answered {gate}",
        )
    if gate.get("result") in ("done", "claimed_shared"):
        failures.append(
            "a foreign reclaim grant took the live claim — the "
            "never-preempts-a-live-holder rule the holderless "
            f"recovery depends on is broken: {gate}"
        )
        raise Abort
    if not claim_reclaim.mutation_fenced(gate):
        raise Inconclusive(
            "reclaim_writer answered neither a grant nor a fencing "
            "refusal — the pinned release predates the grant's "
            "contract shape",
            f"reclaim_writer answered {gate}",
        )
    # The read-only claim probe the post-release assertion reads —
    # a release predating it leaves the holderless shape
    # unobservable.
    probe_w = probe_io.request({"op": "probe_writer"})
    evidence["probe_writer_gate"] = probe_w
    if claim_reclaim.unsupported_verb(probe_w):
        raise Inconclusive(
            "probe_writer answered invalid_request — the pinned "
            "release predates the claim-state probe the "
            "holderless-claim evidence reads",
            f"probe_writer answered {probe_w}",
        )
    if not claim_reclaim.mutation_fenced(probe_w):
        failures.append(
            "probe_writer does not report the standing claim "
            f"held — the claim-state observation answered {probe_w}"
        )
        raise Abort


def holderless_claim_recovery_pass(args, tamper):
    """The holderless-claim-recovery run: launch the unkeyed
    declared-binds pair, converge, gate the contract surface, then
    the two-ex-owner wedge — the involuntary promote, the foreign
    tool claim's fenced demotion, the held window's live-holder
    refusals, the keep-claim release leaving the field unclaimed
    under a holderless claim, and the marked ex-owner's bound
    reclaim landing the pair's field owner back unattended. Returns
    `(digest_entries, evidence, failures)`; raises `Inconclusive`
    where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "holderless-claim-recovery leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    rig = verdict_io = foreign_io = probe_io = None
    a_url = b_url = None
    try:
        rig = foreign_claim_release.launch_unkeyed(args, declared)
        a_url, b_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io
        # The claim attachments the episode stages through — the
        # rig's own client stays read-only: `verdict_io` runs the
        # third-party mutation probes whose fencing verdicts carry
        # the standing claim's declared monitor, `foreign_io` holds
        # the tool claim the second demotion and the holderless
        # release stage, and `probe_io` runs the read-only claim
        # observations and the gate's foreign reclaim ask.
        verdict_io = simulate.PlantClient(rig.plant_addr)
        foreign_io = simulate.PlantClient(rig.plant_addr)
        probe_io = simulate.PlantClient(rig.plant_addr)
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
        # the unkeyed deployment shape the wedge reproduced on.
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

        # Phase 2 — convergence and the contract surface.
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
        a_token = failover.owner_token(rig.duty_preamble)
        if a_token is None:
            raise Inconclusive(
                "the launched active recorded no claim line — the "
                "pinned release claims only on promotion, predating "
                "the claim lifecycle the holderless recovery rides "
                "on"
            )
        gate_contract_surface(
            rig, verdict_io, probe_io, a_token, a_url, failures, evidence
        )
        held0 = failover.field_read(plant_io, cmd, failures)["value"]
        if held0 != simulate.snapshot_point(converged["owner"], cmd):
            failures.append(
                f"the field carries {held0} on the served "
                "out-point while the field owner reports "
                f"{simulate.snapshot_point(converged['owner'], cmd)} "
                "— the settled pair's writes never landed"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "gate",
                "probe": "fenced",
                "owner": "named",
                "monitor": "owner",
                "reclaim": "fenced",
                "claim_probe": "answered",
            }
        )

        # Phase 3 — the first ex-owner: `POST /promote` on the
        # tracking standby while the launch owner still owns the
        # field — the finding's operator entry. The promoted peer's
        # claim preempts; the superseded owner demotes in place,
        # resolves the claim-declared monitor, and re-joins
        # `tracking` — its loss mark cleared by the tracked
        # ownership, the mark's stand-down the wedge's first
        # ex-owner needs.
        transfer = stranded_rejoin.rejoin_cycle(
            rig,
            a_url,
            b_url,
            rig.duty_files,
            True,
            verdict_io,
            failures,
            evidence,
            "transfer",
        )
        digest_entries.append({"phase": "transfer", **transfer})
        if failures:
            raise Abort
        b_token = claim_reclaim.verdict_owner(
            evidence["transfer_verdict"]
        )
        if b_token is None or b_token in (a_token, FOREIGN_CLAIM):
            failures.append(
                "the promoted peer's claim token was not learned "
                f"from the fencing verdict: "
                f"{evidence['transfer_verdict']}"
            )
            raise Abort

        # Phase 4 — the second ex-owner: the dedicated attachment's
        # monitor-less `claim_writer` preempts the promoted peer,
        # whose first fenced write demotes it in place — the
        # loss-marked ex-owner the reclaim probes on — its journal
        # carrying the attributed `field_claim_lost` beside the
        # fenced demote walk.
        floor_a = len(
            pair.get(f"{a_url}/journal", "GET /journal", failures)
        )
        floor_b = len(
            pair.get(f"{b_url}/journal", "GET /journal", failures)
        )
        claim = foreign_io.request(
            {
                "op": "claim_writer",
                "owner": FOREIGN_CLAIM,
                "controller": False,
            }
        )
        evidence["claim"] = claim
        if claim_reclaim.unsupported_verb(claim):
            raise Inconclusive(
                "claim_writer answered invalid_request — the pinned "
                "release predates the claim lifecycle the leg "
                "stages",
                f"claim_writer answered {claim}",
            )
        result = claim.get("result")
        if result == "claimed_shared":
            raise Abort(
                "the foreign claim answered claimed_shared — a live "
                "attachment already holds the leg's fixed token "
                f"{FOREIGN_CLAIM:#x}: {claim}"
            )
        if result != "done":
            raise Inconclusive(
                f"the foreign claim answered {result} — the "
                "consumer harness admits no unconditional "
                "preemption lever, or the pinned plant predates it",
                f"claim_writer answered {claim}",
            )
        seized = verdict_io.request({"op": "step", "dt": 0})
        evidence["seized"] = seized
        if not claim_reclaim.mutation_fenced(seized):
            failures.append(
                "the foreign claim's preempt did not fence the "
                f"field — a third-party probe answered {seized}"
            )
            raise Abort
        if claim_reclaim.verdict_owner(seized) is None:
            raise Inconclusive(
                "the post-preemption fencing verdict names no "
                "standing owner — the pinned release predates the "
                "verdict attribution",
                f"the fencing verdict was {seized}",
            )
        if claim_reclaim.verdict_owner(seized) != FOREIGN_CLAIM:
            failures.append(
                "the fencing verdict attributes the preempted "
                "claim to "
                f"{claim_reclaim.verdict_owner(seized)}, not the "
                f"foreign token {FOREIGN_CLAIM:#x}: {seized}"
            )
            raise Abort
        if "monitor" in ((seized.get("error")) or {}):
            failures.append(
                "the monitor-less foreign claim names a monitor in "
                f"the fencing verdict: {seized}"
            )
            raise Abort
        roles = foreign_claim_release.fenced_demotion(
            rig, b_url, floor_b, failures, evidence, "preempt"
        )
        digest_entries.append(
            {
                "phase": "preempt",
                "seized": failover.probe_kind(seized),
                "watch": roles,
            }
        )
        # The field snapshot the held window guards — taken only
        # now, once no peer owns: the transfer's legitimate writes
        # (the model's own command logic flipping the out-point
        # under the promoted peer) belong to the owning phase, and
        # from here on only a write could move the field — the
        # holderless window's no-foreign-write evidence.
        hold_field = failover.field_read(plant_io, cmd, failures)[
            "value"
        ]

        # Phase 5 — the held window: while the foreign claim keeps
        # its live holder, both ex-owners go orphaned on their pulls
        # and their bound reclaims refuse every scan — the
        # never-preempts-a-live-holder half of the grant's rule. The
        # verdict keeps naming the foreign owner, the field records
        # no write, the shipped tool's census answers while its
        # mutation meets the named fencing refusal, and the
        # orphaned standbys journal their orphan and
        # observed-claimant records.
        hold = []
        ctl_seen = False
        for _ in range(HOLD_SCANS):
            pair.scan(b_url, failures)
            pair.scan(a_url, failures)
            b_role = pair.get(f"{b_url}/role", "GET /role", failures)
            a_role = pair.get(f"{a_url}/role", "GET /role", failures)
            probe = verdict_io.request({"op": "step", "dt": 0})
            held = failover.field_read(plant_io, cmd, failures)[
                "value"
            ]
            b_sync = stranded_rejoin.sync_kind(b_role)
            a_sync = stranded_rejoin.sync_kind(a_role)
            hold.append(
                {
                    "marked": b_role.get("role") + "/" + b_sync,
                    "peer": a_role.get("role") + "/" + a_sync,
                    "claim": (
                        "foreign"
                        if claim_reclaim.verdict_owner(probe)
                        == FOREIGN_CLAIM
                        else "moved"
                    ),
                    "field_moved": held != hold_field,
                }
            )
            if "missing" in (b_sync, a_sync):
                raise Inconclusive(
                    "a standby's role report carries no sync "
                    "vocabulary — the pinned release predates the "
                    "contract's orphaned verdict",
                    f"the role reports were {b_role} / {a_role}",
                )
            if "tracking" in (b_sync, a_sync):
                raise Inconclusive(
                    "an ex-owner reports tracking a line it serves "
                    "unowned — the pinned release predates the "
                    "checkpoint ownership stamp the orphaned "
                    "verdict rides on",
                    f"the role reports were {b_role} / {a_role}",
                )
            if b_role.get("role") != "standby":
                failures.append(
                    "the marked ex-owner left standby while a live "
                    "foreign claim stood — the bound reclaim "
                    "preempted a live holder, the refusal the "
                    f"contract's own rule forbids: {b_role}"
                )
                raise Abort
            if a_role.get("role") != "standby":
                failures.append(
                    "the first ex-owner left standby while a live "
                    "foreign claim stood: "
                    f"{a_role}"
                )
                raise Abort
            if b_sync != "orphaned" or a_sync != "orphaned":
                failures.append(
                    "an ex-owner failed to report the orphaned "
                    "line — the tracked checkpoints stamp no "
                    f"field owner: {b_role} / {a_role}"
                )
                raise Abort
            if not claim_reclaim.mutation_fenced(probe) or (
                claim_reclaim.verdict_owner(probe) != FOREIGN_CLAIM
            ):
                failures.append(
                    "the standing claim moved off the foreign "
                    f"token during the held window: {probe}"
                )
                raise Abort
            if held != hold_field:
                failures.append(
                    "the field moved under the held foreign claim "
                    f"— a foreign write landed: "
                    f"{hold_field} -> {held}"
                )
                raise Abort
            if not ctl_seen:
                ctl_step = plant_ctl(
                    args.plant_ctl, rig.plant_addr, "step", "0"
                )
                ctl_list = plant_ctl(
                    args.plant_ctl, rig.plant_addr, "list"
                )
                evidence["ctl_step"] = ctl_step[2].strip()
                evidence["ctl_list"] = ctl_list[1].strip()
                if ctl_step[0] == 0 or (
                    foreign_claim_release.FENCED_DETAIL
                    not in ctl_step[2]
                ):
                    failures.append(
                        "a dcs-plant-ctl step under the held "
                        "foreign claim was not refused with the "
                        f"named fencing failure — exit "
                        f"{ctl_step[0]}: "
                        f"{ctl_step[2].strip() or ctl_step[1].strip()}"
                    )
                    raise Abort
                if ctl_list[0] != 0 or '"points"' not in ctl_list[1]:
                    failures.append(
                        "dcs-plant-ctl list did not answer the "
                        "field census under the held claim — exit "
                        f"{ctl_list[0]}: {ctl_list[2].strip()}"
                    )
                    raise Abort
                ctl_seen = True
        digest_entries.append({"phase": "hold", "hold": hold})

        # The window's journal evidence — the records whose absence
        # is the pre-contract shape: each ex-owner's orphaned
        # transition, and the first ex-owner's observed-claimant
        # record naming the foreign token its refused probes met
        # (the marked ex-owner's dedup is seeded by its own
        # attributed loss, so it journals no second record).
        a_added = pair.get(
            f"{a_url}/journal", "GET /journal", failures
        )[floor_a:]
        b_added = pair.get(
            f"{b_url}/journal", "GET /journal", failures
        )[floor_b:]
        evidence["hold_a_added"] = a_added
        evidence["hold_b_added"] = b_added
        a_kinds = {
            kind
            for entry in a_added
            for kind in entry.get("event", {})
        }
        b_kinds = {
            kind
            for entry in b_added
            for kind in entry.get("event", {})
        }
        if "field_orphaned" not in a_kinds or (
            "field_orphaned" not in b_kinds
        ):
            raise Inconclusive(
                "an ex-owner's orphaned transition left no "
                "field_orphaned record — the pinned release "
                "predates the journaled orphan contract",
                f"the added kinds were {sorted(a_kinds)} / "
                f"{sorted(b_kinds)}",
            )
        if "field_claim_observed" not in a_kinds:
            raise Inconclusive(
                "the first ex-owner's refused probes left no "
                "field_claim_observed record — the pinned release "
                "predates the observed-claimant journal the "
                "recovery's audit reads",
                f"the added kinds were {sorted(a_kinds)}",
            )
        observed = [
            entry["event"]["field_claim_observed"]
            for entry in a_added
            if "field_claim_observed" in entry.get("event", {})
        ]
        if len(observed) != 1 or (
            observed[0].get("claimant") != FOREIGN_CLAIM
        ):
            failures.append(
                "the first ex-owner journaled the observed "
                f"claimants {observed} — expected exactly one "
                f"record naming the foreign token "
                f"{FOREIGN_CLAIM:#x}"
            )
            raise Abort
        if stranded_rejoin.role_walk(a_added):
            failures.append(
                "the first ex-owner journaled a role change while "
                "the live foreign claim stood — no standby may "
                "promote itself past a held claim"
            )
            raise Abort
        if stranded_rejoin.lost_entries(a_added):
            failures.append(
                "the first ex-owner journaled a fencing loss it "
                "already settled — a standing claim is observed, "
                "not lost twice"
            )
            raise Abort

        # Phase 6 — the hand-back: `release_writer` with
        # `keep_claim` leaves the preemptor's claim standing with
        # no live attachment behind it — the field unclaimed under
        # a holderless claim, the dead-owner shape the finding
        # wedged on. The `phantom-release` tamper lands the same
        # request on an attachment that holds nothing: the field
        # rightly answers `done`, the standing claim survives with
        # its live holder, and the pair must stay ownerless — the
        # doctored leg asserting recovery past a release that
        # never ran.
        if tamper == "phantom-release":
            phantom = simulate.PlantClient(rig.plant_addr)
            try:
                release = phantom.request(
                    {"op": "release_writer", "keep_claim": True}
                )
            finally:
                phantom.close()
        else:
            release = foreign_io.request(
                {"op": "release_writer", "keep_claim": True}
            )
        evidence["release"] = release
        if claim_reclaim.unsupported_verb(release):
            raise Inconclusive(
                "release_writer answered invalid_request — the "
                "pinned release predates the claim lifecycle the "
                "leg stages",
                f"release_writer answered {release}",
            )
        if tamper is None and release.get("result") != "done":
            raise Abort(
                f"the foreign claim's hand-back was refused: "
                f"{release}"
            )
        standing = probe_io.request({"op": "probe_writer"})
        evidence["standing"] = standing
        if claim_reclaim.unsupported_verb(standing):
            raise Inconclusive(
                "probe_writer answered invalid_request — the "
                "pinned release predates the claim-state probe",
                f"probe_writer answered {standing}",
            )
        if failover.probe_kind(standing) == "unclaimed":
            raise Inconclusive(
                "the keep-claim release dropped the claim — the "
                "pinned release predates the yielded hand-back "
                "the holderless wedge rides",
                f"probe_writer answered {standing}",
            )
        if claim_reclaim.mutation_fenced(standing):
            if claim_reclaim.verdict_owner(standing) != FOREIGN_CLAIM:
                failures.append(
                    "the released claim does not stand under the "
                    "foreign token — the fencing verdict names "
                    f"{claim_reclaim.verdict_owner(standing)}: "
                    f"{standing}"
                )
                raise Abort
        else:
            failures.append(
                "the released claim is not the holderless fence "
                "the wedge stages — probe_writer answered "
                f"{standing}"
            )
            raise Abort
        digest_entries.append(
            {"phase": "release", "standing": "holderless"}
        )

        # Phase 7 — the recovery: driven scans on the marked
        # ex-owner only — no operator promote, no other peer's
        # probes raced — land its bound reclaim on the holderless
        # claim inside the bounded window, the `reclaim`-origin
        # walk the contract's own path. The defect grant refused
        # this claim every scan; the bound bound must catch the
        # wedge — the pair staying ownerless is the failure this
        # assertion names.
        watch = []
        promoted = None
        for _ in range(RESOLVE_SCANS):
            pair.scan(b_url, failures)
            report = pair.get(
                f"{b_url}/role", "GET /role", failures
            )
            watch.append(
                report.get("role")
                + "/"
                + stranded_rejoin.sync_kind(report)
            )
            if report.get("role") == "active":
                promoted = report
                break
            if report.get("role") not in ("standby", "promoting"):
                failures.append(
                    f"the marked ex-owner reported "
                    f"{report.get('role')} recovering the freed "
                    "field — it may only walk back toward "
                    f"active: {report}"
                )
                raise Abort
        evidence["reclaim"] = watch
        if promoted is None:
            digest_entries.append(
                {"phase": "reclaim", "watch": watch, "reclaim": "stranded"}
            )
            failures.append(
                "the freed field was never re-seated — the marked "
                "ex-owner's bound reclaim never preempted the "
                "standing claim, and the pair stayed ownerless "
                f"past the claim's release: {watch}"
            )
            raise Abort

        # The re-seated claim: the marked ex-owner's own token, its
        # dialable monitor declared — the field-arbitrated tracking
        # surface the first ex-owner's re-join reads.
        verdict = verdict_io.request({"op": "step", "dt": 0})
        evidence["reclaim_verdict"] = verdict
        if not claim_reclaim.mutation_fenced(verdict):
            failures.append(
                "the re-seated claim does not fence third-party "
                f"mutations: {verdict}"
            )
            raise Abort
        if claim_reclaim.verdict_owner(verdict) != b_token:
            failures.append(
                "the holderless claim did not resolve to the "
                "marked ex-owner — the fencing verdict names "
                f"{claim_reclaim.verdict_owner(verdict)}: "
                f"{verdict}"
            )
            raise Abort
        declared_b = stranded_rejoin.verdict_monitor(verdict)
        if declared_b is None:
            raise Inconclusive(
                "the re-seated claim declares no monitor — the "
                "pinned release predates the claim-declared "
                "monitor the recovery's re-join reads",
                f"the fencing verdict was {verdict}",
            )
        kind_b = monitor_kind(declared_b, monitor_addr(b_url))
        if kind_b == "wildcard":
            raise Inconclusive(
                "the re-seated claim declares its wildcard bind "
                "verbatim — the pinned release predates the "
                "claim-monitor normalization",
                f"the declared monitor was {declared_b}",
            )
        if kind_b != "owner":
            failures.append(
                f"the re-seated claim declares monitor "
                f"{declared_b} — not the reclaimed peer's "
                f"dialable monitor {monitor_addr(b_url)}"
            )
            raise Abort

        # The unattended recovery's journaled walk — under the
        # `reclaim` origin, no operator call having run.
        b_journal = pair.get(
            f"{b_url}/journal", "GET /journal", failures
        )
        walk_b = stranded_rejoin.role_walk(b_journal[floor_b:])
        evidence["reclaim_walk"] = walk_b
        if any(
            origin is None for _from, _to, origin in walk_b
        ):
            raise Inconclusive(
                "the marked ex-owner's role walk carries no "
                "switch origin — the pinned release predates "
                "the attribution fields",
                f"the role walk was {walk_b}",
            )
        if ("standby", "promoting", "reclaim") not in walk_b or (
            "promoting",
            "active",
            "reclaim",
        ) not in walk_b:
            failures.append(
                "the holderless claim's reclaim left no "
                "journaled walk back to active under the "
                "`reclaim` origin — an operator or restart "
                f"path ran instead: {walk_b}"
            )
            raise Abort

        # The first ex-owner's re-join: it pulls the re-seated
        # owner through its adopted tracking source and returns
        # to `tracking` — never probing its own stale token past
        # the standing claim, never leaving standby.
        rejoin = []
        tracked = None
        for _ in range(REJOIN_SCANS):
            pair.scan(a_url, failures)
            report = pair.get(
                f"{a_url}/role", "GET /role", failures
            )
            rejoin.append(
                report.get("role")
                + "/"
                + stranded_rejoin.sync_kind(report)
            )
            if claim_reclaim.tracking(report):
                tracked = report
                break
        evidence["rejoin"] = rejoin
        if tracked is None:
            failures.append(
                "the first ex-owner never re-joined the "
                "re-seated owner — its reports stayed "
                f"{rejoin}"
            )
            raise Abort
        doc_a = pair.get(
            f"{a_url}/checkpoint", "GET /checkpoint", failures
        )
        doc_b = pair.get(
            f"{b_url}/checkpoint", "GET /checkpoint", failures
        )
        evidence["doc_rejoin"] = [doc_a, doc_b]
        if doc_a.get("source_owns_field") is not False:
            failures.append(
                "the re-joined peer serves a checkpoint whose "
                "source_owns_field is "
                f"{doc_a.get('source_owns_field')} — a tracking "
                "peer's honest stamp is non-ownership"
            )
            raise Abort
        if not str(doc_a.get("line_owner")).endswith(
            ":" + stranded_rejoin.monitor_port(b_url)
        ):
            failures.append(
                "the re-joined peer serves line_owner "
                f"{doc_a.get('line_owner')} — expected the "
                f"reclaimed peer on "
                f":{stranded_rejoin.monitor_port(b_url)}"
            )
            raise Abort
        if doc_b.get("source_owns_field") is not True:
            failures.append(
                "the reclaimed peer serves a checkpoint whose "
                "source_owns_field is "
                f"{doc_b.get('source_owns_field')} — the field "
                "owner's honest stamp is ownership"
            )
            raise Abort

        # The settled pair: exactly one active — the reclaimed
        # peer — with the first ex-owner tracking it, identical
        # images across a tracking-first pull train, the field
        # carrying the reclaimed owner's writes, and the first
        # ex-owner's journal proving it never promoted.
        handover = []
        for _ in range(pair.HANDOVER_TICKS):
            _tracked, owner = rig.tick(
                a_url,
                b_url,
                failures,
                diverged="the re-joined pair's images diverged "
                "at tick {tick} — the reclaim was not bumpless",
            )
            handover.append(owner["tick"])
        b_role = pair.get(f"{b_url}/role", "GET /role", failures)
        a_role = pair.get(f"{a_url}/role", "GET /role", failures)
        if b_role.get("role") != "active":
            failures.append(
                "the reclaimed peer never settled active — GET "
                f"/role answers {b_role}"
            )
            raise Abort
        if not claim_reclaim.tracking(a_role):
            failures.append(
                "the first ex-owner did not settle a tracking "
                f"standby — GET /role answers {a_role}"
            )
            raise Abort
        held = failover.field_read(plant_io, cmd, failures)["value"]
        if held != simulate.snapshot_point(owner, cmd):
            failures.append(
                "the re-seated claim fences the reclaimed "
                "owner's own writes — the field carries "
                f"{held} while its image reports "
                f"{simulate.snapshot_point(owner, cmd)}"
            )
            raise Abort
        ctl_step = plant_ctl(args.plant_ctl, rig.plant_addr, "step", "0")
        if ctl_step[0] == 0 or (
            foreign_claim_release.FENCED_DETAIL not in ctl_step[2]
        ):
            failures.append(
                "a dcs-plant-ctl step under the re-seated claim "
                "was not refused with the named fencing failure "
                f"— exit {ctl_step[0]}: "
                f"{ctl_step[2].strip() or ctl_step[1].strip()}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "reclaim",
                "watch": watch,
                "walk": "reclaim",
                "owner": "marked",
                "monitor": "owner",
                "rejoin": "tracked",
                "settled": handover,
            }
        )

        # Phase 8 — the launch roles restored: the documented
        # switch seats the launch owner back on the field — the
        # reclaimed peer demotes onto its configured source and
        # the tracked ex-owner promotes — the pair landing back
        # on the launch layout with the claim re-seated under
        # the owner's own token.
        restored = rig.switch(
            b_url,
            a_url,
            failures,
            demote_what="the reclaimed peer",
            promote_what="the re-joined ex-owner",
        )
        post = verdict_io.request({"op": "step", "dt": 0})
        evidence["post_probe"] = post
        if claim_reclaim.verdict_owner(post) != a_token:
            failures.append(
                "the restored claim does not name the launch "
                f"owner's own token: {post}"
            )
            raise Abort
        restored_role = pair.get(
            f"{a_url}/role", "GET /role", failures
        )
        evidence["restored_at"] = restored_role.get("tick")
        digest_entries.append(
            {
                "phase": "restore",
                "ticks": restored["ticks"],
                "duty_role": "active",
                "standby_role": "tracking",
            }
        )

        # Phase 9 — the durable mirror: the manifest-declared
        # journal files carry the episode — the launch owner's
        # attributed loss and adoption, the marked ex-owner's
        # attributed loss beside its fenced and reclaim walks,
        # and the first ex-owner's orphan and observed-claimant
        # records.
        a_durable = stranded_rejoin.journal_entries(
            rig.duty_files["journal_file"]
        )
        b_durable = stranded_rejoin.journal_entries(
            rig.standby_files["journal_file"]
        )
        a_durable_losses = stranded_rejoin.lost_entries(a_durable)
        b_durable_losses = stranded_rejoin.lost_entries(b_durable)
        evidence["durable_losses"] = [
            a_durable_losses,
            b_durable_losses,
        ]
        if len(a_durable_losses) != 1 or (
            a_durable_losses[0][1].get("claimant") != b_token
        ):
            failures.append(
                "the launch owner's durable journal carries the "
                f"losses {a_durable_losses} — expected one "
                "field_claim_lost attributed to the promoted "
                "peer's token"
            )
            raise Abort
        if len(b_durable_losses) != 1 or (
            b_durable_losses[0][1].get("claimant") != FOREIGN_CLAIM
        ):
            failures.append(
                "the marked ex-owner's durable journal carries "
                f"the losses {b_durable_losses} — expected one "
                "field_claim_lost attributed to the foreign "
                "token"
            )
            raise Abort
        a_durable_kinds = stranded_rejoin.durable_kinds(
            rig.duty_files["journal_file"]
        )
        b_durable_kinds = stranded_rejoin.durable_kinds(
            rig.standby_files["journal_file"]
        )
        for owed in (
            "field_claim_lost",
            "tracking_source_adopted",
            "field_orphaned",
            "field_claim_observed",
        ):
            if owed not in a_durable_kinds:
                failures.append(
                    f"the launch owner's durable journal file "
                    f"carries no {owed} record — the journaled "
                    "evidence the two-ex-owner episode owes"
                )
        for owed in (
            "field_claim_lost",
            "role_changed",
            "field_orphaned",
        ):
            if owed not in b_durable_kinds:
                failures.append(
                    f"the marked ex-owner's durable journal file "
                    f"carries no {owed} record — the journaled "
                    "evidence the recovery owes"
                )
        b_durable_walk = stranded_rejoin.role_walk(b_durable)
        if ("standby", "promoting", "reclaim") not in (
            b_durable_walk
        ) or ("promoting", "active", "reclaim") not in (
            b_durable_walk
        ):
            failures.append(
                "the marked ex-owner's durable journal carries "
                f"the role walk {b_durable_walk} — the "
                "reclaim-origin walk back to active went "
                "unrecorded"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "durable",
                "duty": sorted(a_durable_kinds),
                "standby": sorted(b_durable_kinds),
            }
        )
        evidence["final_tick"] = restored_role.get("tick")
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except (Inconclusive, stranded_rejoin.Inconclusive):
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        # Detach the claim attachments cleanly: release whatever
        # hold each still carries — a hold left standing keeps the
        # field claimed for a dead token — then close. The release
        # drops only the connection's own hold, so it never takes
        # the owner's claim down with it.
        for client in (foreign_io,):
            if client is not None:
                try:
                    client.request({"op": "release_writer"})
                except Exception:
                    pass
        if rig is not None and a_url is not None:
            # Best effort: the launch role layout for the legs
            # behind this one. Give a pending bound reclaim a few
            # scans to land — the freed field may already be
            # re-seating — then the documented operator promote is
            # the fallback on a wedge; a peer moved off standby
            # demotes back. A clean pass moved nothing, so neither
            # fires.
            try:
                for _ in range(RESOLVE_SCANS):
                    if (claim_reclaim.try_role(a_url) or {}).get(
                        "role"
                    ) == "active":
                        break
                    claim_reclaim.try_scan(a_url)
                if (claim_reclaim.try_role(a_url) or {}).get(
                    "role"
                ) != "active":
                    pair.request(f"{a_url}/promote", {})
            except Exception:
                pass
            try:
                if (claim_reclaim.try_role(b_url) or {}).get(
                    "role"
                ) != "standby":
                    pair.request(f"{b_url}/demote", {})
            except Exception:
                pass
        for client in (verdict_io, foreign_io, probe_io):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
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
        "non-holder while the claim keeps its live holder, so the "
        "pair stays ownerless past the release the leg believes "
        "ran and the leg must report the named diagnostic",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = (
            holderless_claim_recovery_pass(args, args.tamper)
        )
    except (Inconclusive, stranded_rejoin.Inconclusive) as inconclusive:
        if args.tamper is not None:
            eprint(
                "holderless-claim-recovery: the doctored hand-back "
                "wanted the pair stranded past the claim's release "
                "— an inconclusive run offers the doctored case no "
                "evidence"
            )
            return 1
        # The digest line carries only the stable reason — the
        # run's own verdicts and endpoints report on stderr, where
        # two identical passes need not share them.
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"holderless-claim-recovery: inconclusive — {detail}")
        print(
            f"holderless-claim-recovery-digest inconclusive — {reason}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"holderless-claim-recovery: {line}")
        return 1
    for failure in failures:
        eprint(f"holderless-claim-recovery: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"holderless-claim-recovery: the {args.tamper} case "
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
        f"holderless-claim-recovery-digest {digest} — the "
        "two-ex-owner wedge left the field unclaimed under a "
        "holderless claim, the marked ex-owner's bound reclaim "
        "preempted it unattended inside the bound, the pair "
        "reconverged to one active plus a tracking standby, and "
        "the launch roles stand at tick "
        f"{evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
