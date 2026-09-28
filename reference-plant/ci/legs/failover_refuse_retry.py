#!/usr/bin/env python3
"""The refused-fire failover-gate retry leg for the reference plant —
the consumer-side proof that an armed standby whose budget-th
self-promotion the field's arbitration refuses does not lose its
armed failover gate: while the convergence proof keeps standing
each later due cycle retries the conditional claim, so once a live
incumbent's unyielded claim drops to a dead-owned standing claim
the gate fires again and the peer promotes automatically — never
stranded after one refusal (WW-ENG-003, WW-LCM-001 — the #1165
refused-fire retry contract the monitor-side tracking test pins
in-workspace, mirrored at the customer boundary).

The failover leg (`ci/legs/failover.py`) proves the budget-th fire
over a dead owner's claim; the failover-proof leg proves the served
standing evidence; the yielded-claim re-arm leg proves the
same-owner re-grant over a deliberately yielded claim. None stages
the fired-but-refused window: a gate that fired, met a live
incumbent's refusal, and must keep the gate rather than disarm.
This leg runs the reproduction on the manifest-declared pair,
ordered by the driven harness — a peer's pulls and applies live
only inside its own `POST /scan`, so the episode needs no wall
clock:

- launches the declared pair with the standby's `failover_budget`
  carried to `--auto-promote`, converges it to `tracking`, and
  gates the contract surface — the launched active's recorded
  owner token, the lifecycle verbs answered, the conditional
  claim's refusal, the checkpoint's ownership stamps, the armed
  peer's served failover evidence, and the durable journal files
  the manifest declares;
- issues the documented demote/promote switch so the
  failover-armed peer holds the field's claim under its own
  token;
- demotes the armed field owner so the claim stands yielded under
  its token, then has a dedicated plant attachment's
  `claim_writer` seize the claim under a foreign incumbent token
  marked as a controller's — the live incumbent whose unyielded
  hold the peer's conditional orphan claim must refuse;
- drives the mutual-standby scans: the demoted tracker produces
  ownerless checkpoints, the armed peer's orphaned applies count
  heartbeat misses, and at the declared budget the gate fires —
  the conditional claim meets the live incumbent's
  `field_claim_failed` refusal, journaled once while the retried
  due cycles keep refusing — the served failover evidence
  reporting the proof still standing past the budget;
- removes the incumbent — the attachment's connection drops, the
  claim standing dead-owned — and drives the retry window: the
  armed gate fires again, the conditional claim preempts the
  dead owner's standing claim, and the peer promotes
  automatically through the same scan;
- asserts through the armed peer's serving monitor and durable
  journal: exactly one `promotion_refused` naming the
  `field_claim_failed` cause at the declared budget, the
  failover-origined `standby → promoting → active` role walk,
  the field's fencing verdict naming the promoted peer's token
  and declared monitor, and the promoted peer's writes landing;
- restores the pair's launch roles: the demoted peer reconverges
  `tracking` on the retried owner, and the documented switch
  seats the launch member back as field owner.

The contract postdates the pinned release line: where the
launched tooling predates it — no recorded owner token, the
claim lifecycle verbs unanswered, a checkpoint without the
ownership stamps, no served failover evidence, the armed peer's
budget-th fire preempting the live incumbent outright, the
refusal left unjournaled, the standing proof voiding past the
budget, or the peer stranded standby once the incumbent's hold
drops — the run's own evidence is the pre-contract shape and the
leg reports `failover-retry-digest inconclusive` rather than
asserting until the manifest repins a release carrying the
contract; every released artifact set predates it until the fix
lands and a release carries it.

Usage:

    failover_refuse_retry.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `failover-retry-digest <sha256>` line prints — the
check runs two passes and compares them
(`failover-refuse-retry-nondeterministic`). A contract violation
reports `failover-retry: …` lines on stderr and exits 1 — the
check's `failover-retry-failed`. `--tamper incumbent-held` keeps
the incumbent's live claim standing through the retry window —
the doctored negative a stranded-gate defect produces honestly —
so the leg's retry assertion must report the peer never promoting
rather than passing an unexercised contract.
"""

import argparse
import hashlib
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import claim_reclaim
import failover
import failover_proof
import pair
import simulate
import stranded_rejoin


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a staging that holds the live incumbent's
# claim through the retry window — the gate's retries refusing on
# the honest verdict — must surface the named diagnostic rather
# than pass an unexercised retry contract.
LEG = {
    "order": 560,
    "title": "the failover refuse-retry leg",
    "passes": "failover-retry-leg",
    "failed": "failover-retry-failed",
    "tampers": [
        {
            "name": "incumbent-held",
            "passed": "an incumbent-held case passed the failover-retry leg",
            "missed": "the incumbent-held case did not report its named diagnostic",
            "evidence": ["the armed peer never promoted across the retry window"],
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


# The driven-scan bounds the episode's phases run: the refused
# window spans the armed budget plus a pad proving each later due
# cycle retries the refusal, the retry window the incumbent's death
# resolves inside a handful of scans, the reconverge and restore
# bounds mirroring the yielded-claim re-arm leg's, and the socket
# settle giving the plant a beat to reap the dropped incumbent's
# hold before the first retried fire.
REFUSE_PAD = 3
RETRY_SCANS = 6
RESTORE_SCANS = 12
DEAD_SETTLE_S = 0.1

# The live incumbent's foreign owner token — a small fixed value
# that cannot collide with a controller's per-process minted token,
# distinct from the tokens the other claim legs stage.
INCUMBENT = 0x1182


def journal_entries(path):
    """A `--journal-file`'s entry records in file order — the durable
    half of the audit."""
    return [
        record
        for kind, record in pair.journal_records(path)
        if kind == "entry"
    ]


def refused_entries(entries):
    """The `promotion_refused` records of a journal entry list — the
    journaled evidence of each distinct refusal cause a fired gate
    met."""
    return [
        entry["event"]["promotion_refused"]
        for entry in entries
        if "promotion_refused" in entry.get("event", {})
    ]


def observed_entries(entries):
    """The `field_claim_observed` records of a journal entry list —
    the claimant a refused conditional probe attributed."""
    return [
        entry["event"]["field_claim_observed"]
        for entry in entries
        if "field_claim_observed" in entry.get("event", {})
    ]


def sync_kind(report):
    """The sync vocabulary a standby's RoleReport carries —
    `tracking`, `orphaned`, `degraded`, `diverged` — or
    `unsynchronized` when the report carries the string form."""
    return stranded_rejoin.sync_kind(report)


def try_role(url):
    """`GET /role` that answers None on transport error — the
    best-effort restore's poll, where a missed answer is data, not
    a harness failure."""
    return claim_reclaim.try_role(url)


def try_scan(url):
    """One driven `POST /scan` that answers None on transport error —
    the restore path only; the pass's own phases use `pair.scan` so
    transport errors are recorded failures."""
    return claim_reclaim.try_scan(url)


def failover_retry_pass(args, tamper):
    """The refused-fire retry run: converge and gate, switch onto the
    armed peer, demote it into the yielded claim, seat a live
    incumbent, drive the refused window, drop the incumbent, assert
    the retried promotion and its journaled evidence, then restore
    the launch roles. Returns `(digest_entries, evidence,
    failures)`; raises `Inconclusive` where the pinned release
    predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "failover-retry leg has nothing to exercise"
        )
    _manifest, _duty_decl, standby_decl = declared
    budget = standby_decl.get("failover_budget")
    if (
        not isinstance(budget, int)
        or isinstance(budget, bool)
        or budget < 1
    ):
        raise Abort(
            "the manifest's standby declares no failover_budget — "
            "the failover-retry leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {"budget": budget}, []
    rig = verdict_io = incumbent_io = foreign_io = None
    try:
        rig = pair.launch_pair(args, declared, auto_promote=budget)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io
        # The claim attachments the episode stages through: the
        # rig's own client stays read-only — `verdict_io` runs the
        # third-party mutation probes and never holds a claim,
        # `foreign_io` runs the different-owner conditional probe
        # gating the verb, and `incumbent_io` holds the live
        # incumbent's unyielded claim the refused fire must meet.
        verdict_io = simulate.PlantClient(rig.plant_addr)
        foreign_io = simulate.PlantClient(rig.plant_addr)
        incumbent_io = simulate.PlantClient(rig.plant_addr)
        with open(args.model) as handle:
            model = json.load(handle)
        points = failover.signal_points(model)
        if points is None:
            raise Abort(
                "the emitted model declares no p101-cmd/level-primary "
                "signal points — the leg has no field output to watch"
            )
        cmd = points["cmd"]

        # Phase 1 — convergence and the contract surface: the
        # launched active's recorded claim token, the lifecycle
        # verbs answered, the standing claim fencing foreign
        # mutations and conditional grants while naming its owner,
        # the checkpoint's ownership stamps, the armed peer's
        # served failover evidence, and the durable journal files
        # the manifest declares. Each absence is the release
        # predating the contract, never a violation of it.
        converged = rig.converge(failures)
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )
        evidence["converged"] = converged["ticks"][-1]
        duty_token = failover.owner_token(rig.duty_preamble)
        if duty_token is None:
            raise Inconclusive(
                "the launched active recorded no claim line — the "
                "pinned release claims only on promotion, predating "
                "the claim lifecycle the refused-fire retry "
                "contract rides on"
            )
        if rig.duty_files.get("journal_file") is None or (
            rig.standby_files.get("journal_file") is None
        ):
            raise Inconclusive(
                "the manifest's pair declares no journal files — "
                "the durable half of the episode audit is absent"
            )
        probe0 = verdict_io.request({"op": "step", "dt": 0})
        evidence["probe0"] = probe0
        if not claim_reclaim.mutation_fenced(probe0):
            failures.append(
                "the field held no writer claim after convergence "
                f"— a third-party probe answered {probe0}, so the "
                "leg has no standing claim for the incumbent to take"
            )
            raise Abort
        if claim_reclaim.verdict_owner(probe0) is None:
            raise Inconclusive(
                "the fencing verdict names no standing owner — the "
                "pinned release predates the verdict attribution "
                f"the contract's claimants read: {probe0}"
            )
        if claim_reclaim.verdict_owner(probe0) != duty_token:
            failures.append(
                "the standing claim names a foreign token, not the "
                f"launch owner — the pair is not in its launch "
                f"claim state: {probe0}"
            )
            raise Abort
        conditional0 = foreign_io.request(
            {"op": "claim_writer_unless_held", "owner": INCUMBENT}
        )
        evidence["conditional0"] = conditional0
        if claim_reclaim.unsupported_verb(conditional0):
            raise Inconclusive(
                "claim_writer_unless_held answered invalid_request — "
                "the pinned release predates the conditional-claim "
                f"verb the refused fire speaks: {conditional0}"
            )
        if conditional0.get("result") in ("done", "claimed_shared"):
            # A live incumbent's unyielded claim refusing the
            # conditional grant is the surface this leg exercises;
            # a grant here preempted it — the refusal surface is
            # already broken. Hand the claim straight back.
            try:
                foreign_io.request({"op": "release_writer"})
            except Exception:
                pass
            failures.append(
                "a foreign token's conditional claim preempted the "
                "settled pair's live incumbent: "
                f"{conditional0}"
            )
            raise Abort
        if not claim_reclaim.mutation_fenced(conditional0):
            failures.append(
                "a foreign token's conditional claim answered "
                f"{conditional0} — neither grant nor the fencing "
                "refusal the contract defines"
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
                "predates the orphaned-apply machinery the "
                f"refused fire counts misses on: {doc0}"
            )
        baseline = converged["standby_role"].get("failover")
        if baseline is None:
            raise Inconclusive(
                "the converged standby's report serves no failover "
                "field — the pinned release predates the served "
                "standing-proof contract the armed gate reads"
            )
        armed = failover_proof.check_failover_evidence(
            converged["standby_role"], budget, failures,
            "the armed baseline",
        )
        if armed != {"converged": True, "misses": 0, "budget": budget}:
            failures.append(
                f"the armed baseline reports failover {armed} — a "
                "tracking standby's standing proof reads `converged: "
                f"true` with the miss run zeroed, `budget` the "
                f"armed {budget}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "gate",
                "probe": "fenced",
                "owner": "named",
                "conditional": "fenced",
                "stamps": "present",
                "armed": armed,
            }
        )

        # Phase 2 — the documented switch: demote the launch owner,
        # promote the converged armed standby, so the
        # failover-armed peer holds the field's claim under its own
        # token — the claim the incumbent then seizes.
        switched = rig.switch(
            duty_url,
            standby_url,
            failures,
            demote_what="the launch owner",
            promote_what="the converged armed standby",
        )
        probe1 = verdict_io.request({"op": "step", "dt": 0})
        evidence["armed_probe"] = probe1
        if not claim_reclaim.mutation_fenced(probe1):
            failures.append(
                "the promoted peer's claim does not fence "
                f"third-party mutations: {probe1}"
            )
            raise Abort
        armed_token = claim_reclaim.verdict_owner(probe1)
        if armed_token is None:
            raise Inconclusive(
                "the post-switch fencing verdict names no standing "
                "owner — the pinned release predates the verdict "
                f"attribution the retry contract reads: {probe1}"
            )
        digest_entries.append(
            {
                "phase": "switch",
                "demote": switched["demote"],
                "promote": switched["promote"],
                "ticks": switched["ticks"],
                "armed": "named",
            }
        )

        # Phase 3 — the live incumbent: demote the armed field
        # owner so the claim stands yielded under its token, then
        # the dedicated attachment's unconditional `claim_writer`
        # seizes it under the incumbent token marked a
        # controller's — the unyielded hold the armed peer's
        # conditional orphan claim must refuse while the
        # attachment lives.
        armed_floor = len(
            pair.get(f"{standby_url}/journal", "GET /journal", failures)
        )
        armed_file_floor = len(
            journal_entries(rig.standby_files["journal_file"])
        )
        demote = rig.demote(
            standby_url, failures, "the armed field owner"
        )
        seize = incumbent_io.request(
            {
                "op": "claim_writer",
                "owner": INCUMBENT,
                "controller": True,
            }
        )
        evidence["seize"] = seize
        if claim_reclaim.unsupported_verb(seize):
            raise Inconclusive(
                "claim_writer answered invalid_request — the pinned "
                "release admits no unconditional claim lever the "
                f"incumbent staging needs: {seize}"
            )
        if seize.get("result") != "done":
            failures.append(
                f"the incumbent's claim_writer answered {seize} — "
                "the leg could not seat the live incumbent's "
                "unyielded claim"
            )
            raise Abort
        seized = verdict_io.request({"op": "step", "dt": 0})
        evidence["seized"] = seized
        if not claim_reclaim.mutation_fenced(seized):
            failures.append(
                "the incumbent's claim does not fence third-party "
                f"mutations: {seized}"
            )
            raise Abort
        seized_owner = claim_reclaim.verdict_owner(seized)
        if seized_owner is None:
            raise Inconclusive(
                "the incumbent-held claim's fencing verdict names "
                "no standing owner — the pinned release predates "
                f"the verdict attribution the retry reads: {seized}"
            )
        if seized_owner != INCUMBENT:
            failures.append(
                "the seized claim attributes to "
                f"{seized_owner}, not the incumbent token "
                f"{INCUMBENT:#x}: {seized}"
            )
            raise Abort
        orphan_doc = pair.get(
            f"{duty_url}/checkpoint", "GET /checkpoint", failures
        )
        evidence["orphan_doc"] = orphan_doc
        if "source_owns_field" not in orphan_doc:
            raise Inconclusive(
                "the demoted tracker serves a checkpoint without "
                "the field-ownership stamps — the pinned release "
                "predates the orphaned-apply machinery the miss "
                f"count rides on: {orphan_doc}"
            )
        if orphan_doc.get("source_owns_field") is not False:
            failures.append(
                "the demoted tracker's checkpoint stamps "
                f"source_owns_field "
                f"{orphan_doc.get('source_owns_field')} — the "
                "armed peer's pulls must land ownerless "
                "checkpoints to count the heartbeat misses"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "seize",
                "demote": demote,
                "claim": "granted",
                "verdict": "fenced",
                "owner": "incumbent",
            }
        )

        # Phase 4 — the refused window: mutual-standby scans —
        # the demoted tracker producing ownerless checkpoints,
        # the armed peer's orphaned applies counting heartbeat
        # misses — until the budget-th fire meets the live
        # incumbent's refusal, and a pad of further due cycles
        # proves each retried fire keeps refusing rather than
        # disarming or preempting.
        watch = []
        armed_promoted = False
        bound = budget + REFUSE_PAD
        for _ in range(bound):
            pair.scan(duty_url, failures)
            pair.scan(standby_url, failures)
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            partner = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            if report.get("role") in ("active", "promoting"):
                armed_promoted = True
                break
            if partner.get("role") in ("active", "promoting"):
                failures.append(
                    "the demoted tracker left standby through "
                    "the refused window — a wrong peer took the "
                    f"field: {partner}"
                )
                raise Abort
            served = report.get("failover") or {}
            watch.append(
                {
                    "armed": str(report.get("role"))
                    + "/"
                    + sync_kind(report),
                    "duty": str(partner.get("role")),
                    "misses": served.get("misses"),
                    "converged": served.get("converged"),
                }
            )
        evidence["refuse_watch"] = watch
        if armed_promoted:
            raise Inconclusive(
                "the armed peer's fired self-promotion preempted "
                "the live incumbent's unyielded claim — the "
                "pinned release's orphan promotion predates the "
                "conditional claim the refusal contract rides on"
            )
        if watch[-1]["armed"].split("/")[0] != "standby":
            raise Inconclusive(
                "the armed peer never settled standby after its "
                "demote — the pinned release's demote path "
                f"predates the yielded hand-off: {watch[-4:]}"
            )
        orphaned = any(
            row["armed"].endswith("/orphaned") for row in watch
        )
        peak = max(
            (
                row["misses"]
                for row in watch
                if isinstance(row["misses"], int)
                and not isinstance(row["misses"], bool)
            ),
            default=None,
        )
        if peak is None or peak < budget:
            if not orphaned:
                failures.append(
                    "the armed peer never reported orphaned on "
                    "the ownerless tracked line — its apply path "
                    f"refused the wedge's checkpoints: "
                    f"{watch[-4:]}"
                )
                raise Abort
            raise Inconclusive(
                "the armed peer's orphaned applies never counted "
                "the declared budget's miss boundary — the pinned "
                "release predates the orphaned-miss accounting "
                f"the gate fires on: {watch[-4:]}"
            )
        if watch[-1]["converged"] is not True:
            raise Inconclusive(
                "the armed peer's standing proof voided past the "
                "budget — the pinned release predates the "
                "refused-fire retry contract's live gate: "
                f"{watch[-4:]}"
            )
        verdict = verdict_io.request({"op": "step", "dt": 0})
        evidence["held"] = verdict
        if not claim_reclaim.mutation_fenced(verdict):
            failures.append(
                "the incumbent's live claim stopped fencing "
                f"third-party mutations mid-window: {verdict}"
            )
            raise Abort
        if claim_reclaim.verdict_owner(verdict) != INCUMBENT:
            failures.append(
                "the standing claim moved off the incumbent "
                "through the refused window — the refusal cause "
                f"the gate must retry against changed: {verdict}"
            )
            raise Abort

        # The fired-but-refused attempt's durable evidence:
        # exactly one `promotion_refused` naming the
        # `field_claim_failed` cause at the declared budget's
        # miss count — the dedup a standing refusal cause
        # journals once, however many due cycles retried — on
        # both the served tail and the durable file.
        armed_added = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )[armed_floor:]
        served_refusals = refused_entries(armed_added)
        file_refusals = refused_entries(
            journal_entries(rig.standby_files["journal_file"])[
                armed_file_floor:
            ]
        )
        evidence["served_refusals"] = served_refusals
        evidence["file_refusals"] = file_refusals
        if not served_refusals or not file_refusals:
            raise Inconclusive(
                "the refused self-promotion left no "
                "promotion_refused record — the pinned release "
                "predates the journaled-refusal evidence the "
                "retry contract reads: served "
                f"{served_refusals} durable {file_refusals}"
            )
        if len(served_refusals) != 1 or len(file_refusals) != 1:
            failures.append(
                "the refused streak journaled "
                f"{len(served_refusals)} served and "
                f"{len(file_refusals)} durable promotion_refused "
                "records — a standing refusal cause journals "
                "once, not once per retried scan"
            )
            raise Abort
        if served_refusals != file_refusals:
            failures.append(
                "the served and durable refusal records "
                "disagree — the two audit surfaces of the one "
                f"episode diverge: {served_refusals} vs "
                f"{file_refusals}"
            )
            raise Abort
        refusal = served_refusals[0]
        if "field_claim_failed" not in (refusal.get("error") or {}):
            failures.append(
                "the journaled refusal names "
                f"{refusal.get('error')} — the live incumbent's "
                "unyielded claim is the field_claim_failed cause"
            )
            raise Abort
        misses_fired = refusal.get("misses")
        if (
            not isinstance(misses_fired, int)
            or isinstance(misses_fired, bool)
            or misses_fired < budget
        ):
            failures.append(
                "the journaled refusal fired at misses="
                f"{misses_fired} — below the declared budget "
                f"{budget} the record mis-attributes the "
                "boundary it fired at"
            )
            raise Abort
        # The orphan cycle's own observation: the demoted
        # ex-owner's refused conditional re-arm attributed the
        # live incumbent — the episode's claimant record.
        observed = observed_entries(armed_added)
        incumbent_observed = [
            record
            for record in observed
            if record.get("claimant") == INCUMBENT
        ]
        if observed and not incumbent_observed:
            failures.append(
                "the journaled field_claim_observed names "
                f"{observed} — none attributes the incumbent "
                f"token {INCUMBENT:#x} the refused probes met"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "refuse",
                "watch": watch,
                "refusal": {
                    "error": "field_claim_failed",
                    "misses": misses_fired,
                },
                "observed": "incumbent" if incumbent_observed
                else "absent",
            }
        )

        # Phase 5 — the incumbent dies: its connection's end
        # drops its hold, the claim standing dead-owned — the
        # preemptable state the armed gate was kept for. The
        # settle beat lets the plant reap the dropped hold
        # before the first retried fire. The doctored case
        # keeps the attachment live — the claim never goes
        # dead-owned, so every retry keeps refusing: the
        # stranded outcome the leg's promotion assertion must
        # then report.
        if tamper == "incumbent-held":
            dead_owned = False
        else:
            incumbent_io.close()
            incumbent_io = None
            dead_owned = True
            time.sleep(DEAD_SETTLE_S)
        retry_watch = []
        promoted = None
        promoted_snapshot = None
        for _ in range(RETRY_SCANS):
            pair.scan(duty_url, failures)
            snap = pair.scan(standby_url, failures)
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            partner = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            retry_watch.append(
                str(report.get("role"))
                + "/"
                + sync_kind(report)
                + "|"
                + str(partner.get("role"))
            )
            if partner.get("role") in ("active", "promoting"):
                failures.append(
                    "the demoted tracker left standby through "
                    "the retry window — a wrong peer took the "
                    f"field: {partner}"
                )
                raise Abort
            if report.get("role") == "active":
                promoted = report
                promoted_snapshot = snap
                break
        evidence["retry_watch"] = retry_watch
        if promoted is None:
            if tamper == "incumbent-held":
                failures.append(
                    "the armed peer never promoted across the "
                    "retry window — the doctored staging held "
                    "the incumbent's live claim, so every "
                    "retried fire stayed refused"
                )
                raise Abort
            raise Inconclusive(
                "the armed peer stayed stranded standby after "
                "the incumbent's death — its claim standing "
                "dead-owned while the due gate should have "
                "retried and promoted: the pinned release "
                "predates the refused-fire retry contract"
            )
        digest_entries.append(
            {
                "phase": "retry",
                "incumbent": "dead-owned" if dead_owned else "held",
                "outcome": "promoted",
            }
        )

        # Phase 6 — the retried landing: the promoted peer's own
        # conditional claim preempted the dead-owned standing
        # claim — the fencing verdict names its token and the
        # monitor it declared, its first field-owning write
        # lands, and its journal carries the failover-origined
        # walk beside the one refusal record the episode
        # journaled.
        verdict = verdict_io.request({"op": "step", "dt": 0})
        evidence["promoted_verdict"] = verdict
        if not claim_reclaim.mutation_fenced(verdict):
            failures.append(
                "the retried promotion did not seat the armed "
                "peer's claim — a third-party probe answered "
                f"{verdict}"
            )
            raise Abort
        if claim_reclaim.verdict_owner(verdict) != armed_token:
            failures.append(
                "the retried claim attributes to "
                f"{claim_reclaim.verdict_owner(verdict)}, not "
                "the armed peer's own token re-seated over the "
                f"dead-owned claim: {verdict}"
            )
            raise Abort
        declared_monitor = stranded_rejoin.verdict_monitor(verdict)
        if declared_monitor is None:
            raise Inconclusive(
                "the retried claim declares no monitor — the "
                "pinned release predates the claim-declared-"
                "monitor field the audit reads"
            )
        if not str(declared_monitor).endswith(
            ":" + stranded_rejoin.monitor_port(standby_url)
        ):
            failures.append(
                "the retried claim declares monitor "
                f"{declared_monitor}, not the promoted armed "
                f"peer on "
                f":{stranded_rejoin.monitor_port(standby_url)}"
            )
            raise Abort
        held = failover.field_read(plant_io, cmd, failures)["value"]
        if held != simulate.snapshot_point(promoted_snapshot, cmd):
            failures.append(
                "the retried promotion's first field-owning "
                f"scan left the field at {held} while its "
                f"image reports "
                f"{simulate.snapshot_point(promoted_snapshot, cmd)} "
                "— its writes do not land, so the claim did "
                "not move to it"
            )
            raise Abort
        armed_added = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )[armed_floor:]
        walk = stranded_rejoin.role_walk(armed_added)
        file_walk = stranded_rejoin.role_walk(
            journal_entries(rig.standby_files["journal_file"])[
                armed_file_floor:
            ]
        )
        if any(origin is None for _frm, _to, origin in walk):
            raise Inconclusive(
                "the served journal's role changes carry no "
                "origin attribution — the pinned release "
                "predates the walk fields the failover-audit "
                f"reads: {walk}"
            )
        if walk != file_walk:
            failures.append(
                "the durable role walk diverges from the "
                f"served: {file_walk} vs {walk}"
            )
            raise Abort
        want_tail = [
            ("standby", "promoting", "failover"),
            ("promoting", "active", "failover"),
        ]
        if walk[-2:] != want_tail:
            failures.append(
                "the armed peer's role walk ends "
                f"{walk[-2:]} — the retried fire's promotion "
                "must journal standby → promoting → active "
                "with the failover origin, actorless"
            )
            raise Abort
        if len(refused_entries(armed_added)) != 1:
            failures.append(
                "the retried window journaled more than the "
                "one standing refusal cause — a retrying gate "
                "must not flood the trail"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "walk": walk,
                "verdict": "fenced",
                "owner": "owner",
                "monitor": "named",
                "field": "landed",
                "refusals": 1,
            }
        )

        # Phase 7 — the restore: the demoted peer reconverges
        # `tracking` on the retried owner's now-owned
        # checkpoint stream, and the documented switch seats
        # the launch member back as field owner — the pair's
        # launch roles for the legs behind this one.
        reconverge = []
        tracked = None
        for _ in range(RESTORE_SCANS):
            pair.scan(standby_url, failures)
            pair.scan(duty_url, failures)
            duty_role = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            reconverge.append(sync_kind(duty_role))
            if claim_reclaim.tracking(duty_role):
                tracked = duty_role
                break
        evidence["reconverge"] = reconverge
        if tracked is None:
            failures.append(
                "the demoted peer never reconverged tracking "
                f"on the retried owner — its sync stayed "
                f"{reconverge}"
            )
            raise Abort
        rig.demote(standby_url, failures, "the retried owner")
        rig.promote(
            duty_url, failures, "the restored launch member"
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
                str(duty_role.get("role"))
                + "/"
                + str(peer_role.get("role"))
            )
            if duty_role.get("role") == "active" and (
                claim_reclaim.tracking(peer_role)
            ):
                done = True
                break
        evidence["restore"] = restored
        if not done:
            failures.append(
                "the launch layout never restored — the "
                f"pair's role reports stayed {restored}"
            )
            raise Abort
        post = verdict_io.request({"op": "step", "dt": 0})
        evidence["post_probe"] = post
        if not claim_reclaim.mutation_fenced(post):
            failures.append(
                "the restored claim does not fence "
                f"third-party mutations: {post}"
            )
            raise Abort
        declared_monitor = stranded_rejoin.verdict_monitor(post)
        if declared_monitor is not None:
            if not str(declared_monitor).endswith(
                ":" + stranded_rejoin.monitor_port(duty_url)
            ):
                failures.append(
                    "the restored claim declares monitor "
                    f"{declared_monitor}, not the rejoined "
                    f"duty member on "
                    f":{stranded_rejoin.monitor_port(duty_url)}"
                )
                raise Abort
            seated = "named"
        else:
            seated = (
                "owner"
                if claim_reclaim.verdict_owner(post)
                not in (None, armed_token, INCUMBENT)
                else "foreign"
            )
            if seated != "owner":
                failures.append(
                    "the restored claim names "
                    f"{claim_reclaim.verdict_owner(post)} — "
                    "never re-seated under the rejoined duty "
                    f"member: {post}"
                )
                raise Abort
        digest_entries.append(
            {
                "phase": "restore",
                "reconverge": reconverge,
                "watch": restored,
                "seated": seated,
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
        # hold each still carries — a hold left standing keeps
        # the field claimed for a dead token — then close. The
        # release drops only the connection's own hold, so it
        # never takes the owner's claim down with it.
        for client in (verdict_io, foreign_io, incumbent_io):
            if client is not None:
                try:
                    client.request({"op": "release_writer"})
                except Exception:
                    pass
                client.close()
        if rig is not None:
            # Best effort: the launch role layout for the legs
            # behind this one — a clean pass already restored
            # it. A dead member relaunches as the standby its
            # tracking wiring names; a peer still owning the
            # field demotes back once the rejoined peer tracks
            # it. The armed peer left armed and due after the
            # released incumbent may self-promote across the
            # restore's own scans — the same demote/promote
            # sequence unwinds that landing too.
            try:
                duty_report = (
                    try_role(rig.duty_url) if rig.duty_url else None
                )
                armed_report = (
                    try_role(rig.standby_url)
                    if rig.standby_url
                    else None
                )
                if (duty_report or {}).get("role") != "active":
                    if rig.duty is None or rig.duty.poll() is not None:
                        target = (
                            rig.standby_url.removeprefix("http://")
                            if rig.standby_url
                            else None
                        )
                        listen = (
                            rig.duty_url.removeprefix("http://")
                            if rig.duty_url
                            else "127.0.0.1:0"
                        )
                        proc, url, _pre = pair.spawn_peer(
                            args.controller,
                            args.model,
                            args.dt,
                            rig.plant_addr,
                            target,
                            rig.duty_files,
                            listen=listen,
                            pair_token=pair.PAIR_TOKEN,
                        )
                        if url is not None:
                            rig.duty, rig.duty_url = proc, url
                    for _ in range(RESTORE_SCANS):
                        if rig.standby_url:
                            try_scan(rig.standby_url)
                        if rig.duty_url:
                            try_scan(rig.duty_url)
                    armed_report = (
                        try_role(rig.standby_url)
                        if rig.standby_url
                        else None
                    )
                    duty_report = (
                        try_role(rig.duty_url)
                        if rig.duty_url
                        else None
                    )
                    if (armed_report or {}).get("role") == "active" and (
                        duty_report or {}
                    ).get("role") == "standby":
                        pair.request(f"{rig.standby_url}/demote", {})
                    for _ in range(RESTORE_SCANS):
                        if rig.duty_url:
                            try_scan(rig.duty_url)
                        duty_report = (
                            try_role(rig.duty_url)
                            if rig.duty_url
                            else None
                        )
                        if (duty_report or {}).get("role") == "standby":
                            status, _body = pair.request(
                                f"{rig.duty_url}/promote", {}
                            )
                            if status != 200:
                                break
                        else:
                            break
                    for _ in range(RESTORE_SCANS):
                        if rig.standby_url:
                            try_scan(rig.standby_url)
                        if rig.duty_url:
                            try_scan(rig.duty_url)
            except Exception:
                pass
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
        choices=["incumbent-held"],
        help="keep the incumbent's live claim through the retry "
        "window — the leg must report the peer never promoting",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = failover_retry_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "failover-retry: the armed peer never promoted "
                "across the retry window is the doctored case's "
                "proof — an inconclusive run offers it no evidence"
            )
            return 1
        eprint(f"failover-retry: inconclusive — {inconclusive}")
        print(f"failover-retry-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"failover-retry: {line}")
        return 1
    for failure in failures:
        eprint(f"failover-retry: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"failover-retry: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "staging"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"failover-retry-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the live incumbent refused the "
        "budget-th fire once, the dead-owned claim's retry "
        "promoted the armed peer, and the launch roles restored"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
