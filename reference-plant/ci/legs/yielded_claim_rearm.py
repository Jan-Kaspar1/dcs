#!/usr/bin/env python3
"""The yielded-claim re-arm leg for the reference plant — the
consumer-side proof that a demoted owner's yielded claim re-arms
when that same owner re-grants itself into a live controller hold:
a different owner's conditional claim then answers fenced while
the re-bound incumbent keeps the field, rather than the
deliberate hand-off staying preemptable behind its own standing
attachment (WW-ENG-003, WW-LCM-001 — the #1123
yielded-claim-regrant contract the rig's yielded-rearm scenario
pins in-workspace, mirrored at the customer boundary).

The yield mark is a hand-off signal, not a state: the demotion's
`release_writer{keep_claim}` leaves the standing claim yielded so
a successor's conditional claim can preempt the deliberate
hand-off despite other holders — but once the same owner
re-grants itself into a live *controller* hold the mark must end,
re-arming the live-incumbent refusal `claim_writer_unless_held`
exists to give a restarted peer's stale-resume claim. Before the
contract the mark never cleared: every later conditional claimer
preempted the live re-bound incumbent, so a restarted,
previously-demoted controller could seize the field from a live
failover successor with no operator action.

The claim-reclaim leg (`ci/legs/claim_reclaim.py`) proves the
foreign preempt-and-reclaim lifecycle; the stranded-rejoin leg
proves the involuntary demotion's verified re-join. Neither
stages the same-owner re-grant over a yielded claim. This leg
runs the rig's own failover reproduction on the
manifest-declared pair, ordered by the driven harness — a peer's
pulls and applies live only inside its own `POST /scan`, so the
episode needs no wall clock:

- launches the declared pair with the standby's
  `failover_budget` carried to `--auto-promote`, converges it to
  `tracking`, and gates the contract surface — the launched
  active's recorded owner token, the lifecycle verbs answered,
  the fencing verdict's owner and declared-monitor attribution,
  and the checkpoint's ownership stamps;
- issues the documented demote/promote switch so the
  failover-armed peer holds the field's claim under its own
  token;
- demotes the armed field owner — the deliberate hand-off
  `release_writer{keep_claim}` stages: the claim stands yielded
  under the armed peer's token, still fencing third-party
  mutations and naming that token;
- drives the armed peer's orphan failover: its tracking scans
  pull the demoted tracker's ownerless checkpoints — each
  orphaned apply a heartbeat miss — until the budget-th apply
  self-promotes it over its own yielded claim — the same-owner
  conditional re-grant the contract re-arms;
- restarts the ex-owner's controller service: the demoted launch
  member relaunches as its configured active, and its startup
  conditional claim must meet the re-armed incumbent's refusal
  and exit rather than preempting the live successor — through
  the window the re-promoted peer stays `active`, its writes
  keep landing, and every fencing verdict keeps naming its
  token;
- asserts through both peers' serving monitors and durable
  journals: the re-promoted peer journals no `field_claim_lost`
  and no `origin: "fenced"` demotion through the restartee
  window, the restartee's durable journal gains no run records
  past the fenced start's recorded stand-down evidence — the
  observed incumbent's claim attribution and the
  `active -> standby` stand-down — and a foreign
  `claim_writer_unless_held` probe answers fenced naming the
  re-granted incumbent;
- restores the pair's launch roles: the ex-owner rejoins as
  `--standby` — the documented remedy the refusal names —
  reconverges to `tracking`, and the documented switch seats it
  back as field owner.

The contract postdates the pinned release line: where the
launched tooling predates it — no recorded owner token, the
claim lifecycle verbs unanswered, a fencing verdict naming no
standing owner, a checkpoint without the ownership stamps, the
demote dissolving rather than yielding the claim, or the orphan
failover never re-promoting — the run's own evidence is the
pre-contract shape and the leg reports
`yielded-claim-rearm-digest inconclusive` rather than asserting
until the manifest repins a release carrying the contract; the
v0.3.0 artifact set is one.

Usage:

    yielded_claim_rearm.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `yielded-claim-rearm-digest <sha256>` line prints —
the check runs two passes and compares them
(`yielded-claim-rearm-nondeterministic`). A contract violation
reports `yielded-claim-rearm: …` lines on stderr and exits 1 —
the check's `yielded-claim-rearm-failed`. `--tamper
expect-granted` doctors the leg's own expectation to the defect
shape — asserting the foreign conditional claim was granted over
the re-bound incumbent — so the leg proves its fencing assertion
fires rather than passing an unexercised contract.
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
import pair
import simulate
import stranded_rejoin


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the foreign conditional claim
# was granted over the re-bound incumbent — the defect the
# contract closed — must surface the named diagnostic on the
# honest fenced verdict rather than passing an unexercised
# contract.
LEG = {
    "order": 500,
    "title": "the yielded-claim re-arm leg",
    "passes": "yielded-claim-rearm",
    "tampers": [
        {
            "name": "expect-granted",
            "passed": "an expect-granted case passed the yielded-claim-rearm leg",
            "missed": "the expect-granted case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted the foreign claim granted"],
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


# The driven-scan bounds the episode's phases run: the armed peer's
# demote settles standby inside a couple of scans, the orphan
# re-promotion lands on the budget-th orphaned apply plus a small
# pad, the restartee observation window spans a handful of driven
# owner scans, and the restore's reconvergence bound mirrors the
# stranded-rejoin leg's.
DEMOTE_SCANS = 6
ORPHAN_PAD = 4
WINDOW_ROUNDS = 4
RESTORE_SCANS = 12

# The foreign owner token the contender probe claims under — a
# small fixed value that cannot collide with a controller's
# per-process minted token, distinct from the tokens the other
# claim legs stage.
FOREIGN_OWNER = 0xF041

# The startup-refusal wording a fenced conditional startup claim
# exits naming — the launched active's `FieldClaimFailed` report a
# live incumbent's unyielded claim produces.
REFUSAL_WORDING = "a live peer holds the field's write-ownership claim"


def journal_entries(path):
    """A `--journal-file`'s entry records in file order — the durable
    half of the audit."""
    return [
        record
        for kind, record in pair.journal_records(path)
        if kind == "entry"
    ]


def sync_kind(report):
    """The sync vocabulary a standby's RoleReport carries —
    `tracking`, `orphaned`, `degraded`, `diverged` — or
    `unsynchronized` when the report carries the string form."""
    return stranded_rejoin.sync_kind(report)


def owner_word(token, armed_token):
    """The claim-identity word a fencing verdict's owner reads as in
    the digest — the re-granted incumbent's token, the leg's foreign
    token, nobody, or another — never the raw per-process value two
    identical passes cannot share."""
    if token == armed_token:
        return "owner"
    if token == FOREIGN_OWNER:
        return "foreign"
    if token is None:
        return "unowned"
    return "other"


def try_role(url):
    """`GET /role` that answers None on transport error — the
    restartee poll, where a dead monitor is data, not a harness
    failure."""
    return claim_reclaim.try_role(url)


def try_scan(url):
    """One driven `POST /scan` that answers None on transport error —
    the restore path only; the pass's own phases use `pair.scan` so
    transport errors are recorded failures."""
    return claim_reclaim.try_scan(url)


def fenced_naming(verdict, token):
    """Whether a mutation probe answers the fencing verdict naming
    `token` as the standing claim's owner."""
    return claim_reclaim.mutation_fenced(verdict) and (
        claim_reclaim.verdict_owner(verdict) == token
    )


def yielded_claim_rearm_pass(args, tamper):
    """The yielded-claim re-arm run: converge and gate, switch onto
    the armed peer, demote it into the yielded claim, drive the
    orphan re-promotion, restart the ex-owner as its configured
    active, assert the fenced window and the journal audits, then
    restore the launch roles. Returns `(digest_entries, evidence,
    failures)`; raises `Inconclusive` where the pinned release
    predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "yielded-claim re-arm leg has nothing to exercise"
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
            "the yielded-claim re-arm leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = verdict_io = foreign_io = None
    try:
        rig = pair.launch_pair(args, declared, auto_promote=budget)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io
        # The claim attachments the episode stages through: the
        # rig's own client stays read-only — `verdict_io` runs the
        # third-party mutation probes and never holds a claim,
        # `foreign_io` runs the different-owner conditional probe
        # the re-armed claim must refuse.
        verdict_io = simulate.PlantClient(rig.plant_addr)
        foreign_io = simulate.PlantClient(rig.plant_addr)
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
        # launched active's recorded claim token, the standing
        # claim fencing foreign mutations and conditional grants
        # while naming its owner, the checkpoint's ownership
        # stamps, and the durable journal files the manifest
        # declares. Each absence is the release predating the
        # contract, never a violation of it.
        converged = rig.converge(failures)
        owner = converged["owner"]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )
        evidence["converged"] = converged["ticks"][-1]
        owner_token = failover.owner_token(rig.duty_preamble)
        if owner_token is None:
            raise Inconclusive(
                "the launched active recorded no claim line — the "
                "pinned release claims only on promotion, predating "
                "the claim lifecycle the yielded-claim re-arm "
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
                "leg has no standing claim to yield"
            )
            raise Abort
        if claim_reclaim.verdict_owner(probe0) is None:
            raise Inconclusive(
                "the fencing verdict names no standing owner — the "
                "pinned release predates the verdict attribution "
                f"the contract's claimants read: {probe0}"
            )
        if stranded_rejoin.verdict_monitor(probe0) is None:
            raise Inconclusive(
                "the fencing verdict declares no incumbent "
                "monitor — the pinned release predates the "
                "claim-declared-monitor contract the restore's "
                f"re-seat check reads: {probe0}"
            )
        if claim_reclaim.verdict_owner(probe0) != owner_token:
            failures.append(
                "the standing claim names a foreign token, not the "
                f"launch owner — the pair is not in its launch "
                f"claim state: {probe0}"
            )
            raise Abort
        conditional0 = foreign_io.request(
            {"op": "claim_writer_unless_held", "owner": FOREIGN_OWNER}
        )
        evidence["conditional0"] = conditional0
        if claim_reclaim.unsupported_verb(conditional0):
            raise Inconclusive(
                "claim_writer_unless_held answered invalid_request — "
                "the pinned release predates the conditional-claim "
                f"verb the re-arm contract re-arms: {conditional0}"
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
                f"re-grant stages on: {doc0}"
            )
        digest_entries.append(
            {
                "phase": "gate",
                "probe": "fenced",
                "owner": "named",
                "conditional": "fenced",
                "stamps": "present",
            }
        )

        # Phase 2 — the documented switch: demote the launch owner,
        # promote the converged armed standby, so the
        # failover-armed peer holds the field's claim under its own
        # token — the claim the leg's demote then yields.
        switched = rig.switch(
            duty_url,
            standby_url,
            failures,
            demote_what="the launch owner",
            promote_what="the converged armed standby",
        )
        owner = switched["owner"]
        evidence["switched_at"] = switched["demote"]["tick"]
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
                f"attribution the re-arm contract reads: {probe1}"
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

        # Phase 3 — the yield and the same-owner re-grant: demote
        # the armed field owner so the claim stands yielded under
        # its token, then drive the mutual-standby scans — the
        # demoted tracker producing ownerless checkpoints, the
        # armed peer's orphaned applies counting heartbeat misses —
        # until the budget-th apply self-promotes the armed peer
        # over its own yielded claim.
        armed_floor = len(
            pair.get(f"{standby_url}/journal", "GET /journal", failures)
        )
        armed_file_floor = len(
            journal_entries(rig.standby_files["journal_file"])
        )
        demote = rig.demote(
            standby_url, failures, "the armed field owner"
        )
        watch = []
        repromoted = None
        yielded_probe = None
        bound = DEMOTE_SCANS + budget + ORPHAN_PAD
        for _ in range(bound):
            # The tracked peer's scan produces the ownerless
            # checkpoint the armed peer's pull then applies —
            # each orphaned apply the heartbeat miss the armed
            # failover counts.
            pair.scan(duty_url, failures)
            pair.scan(standby_url, failures)
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            partner = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            watch.append(
                str(report.get("role"))
                + "/"
                + sync_kind(report)
                + "|"
                + str(partner.get("role"))
            )
            if partner.get("role") in ("active", "promoting"):
                failures.append(
                    "the demoted tracker left standby through the "
                    f"orphan window — a wrong peer took the field: "
                    f"{partner}"
                )
                raise Abort
            if yielded_probe is None and (
                report.get("role") == "standby"
            ):
                yielded_probe = verdict_io.request(
                    {"op": "step", "dt": 0}
                )
                evidence["yielded_probe"] = yielded_probe
                if not claim_reclaim.mutation_fenced(yielded_probe):
                    raise Inconclusive(
                        "the armed peer's demote dissolved the "
                        "claim rather than leaving it yielded — the "
                        "pinned release predates the yield "
                        "lifecycle the re-arm contract stands on: "
                        f"{yielded_probe}"
                    )
                if claim_reclaim.verdict_owner(
                    yielded_probe
                ) != armed_token:
                    failures.append(
                        "the yielded claim moved off the armed "
                        "peer's token — the demoted hand-off "
                        "attributes to "
                        f"{claim_reclaim.verdict_owner(yielded_probe)}: "
                        f"{yielded_probe}"
                    )
                    raise Abort
            if report.get("role") == "active":
                repromoted = report
                break
        evidence["orphan_watch"] = watch
        if yielded_probe is None:
            raise Inconclusive(
                "the armed peer never settled standby after its "
                "demote — the pinned release's demote path predates "
                f"the yielded hand-off: {watch[-4:]}"
            )
        if repromoted is None:
            raise Inconclusive(
                "the armed orphan never re-promoted over its own "
                "yielded claim inside the failover budget — the "
                "pinned release predates the orphan-failover claim "
                f"path the same-owner re-grant rides on: {watch[-4:]}"
            )
        reseated = verdict_io.request({"op": "step", "dt": 0})
        evidence["reseated"] = reseated
        if not fenced_naming(reseated, armed_token):
            failures.append(
                "the orphan re-promotion did not re-seat the armed "
                "peer's claim — a third-party probe answered "
                f"{reseated}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "yield",
                "demote": demote,
                "watch": watch,
                "yielded": "named",
                "reseat": "named",
            }
        )

        # The demoted tracker reconverges on the re-armed owner's
        # now-owned checkpoint stream — the mutual-standby wedge
        # resolving back to one active plus one tracking peer.
        reconverge = []
        tracked = None
        for _ in range(RESTORE_SCANS):
            pair.scan(standby_url, failures)
            pair.scan(duty_url, failures)
            report = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            reconverge.append(sync_kind(report))
            if claim_reclaim.tracking(report):
                tracked = report
                break
        evidence["reconverge"] = reconverge
        if tracked is None:
            failures.append(
                "the demoted peer never reconverged tracking on "
                f"the re-armed owner — its sync stayed {reconverge}"
            )
            raise Abort

        # Phase 4 — the restartee: the demoted launch member
        # cold-restarts as its configured active — the service
        # relaunch keeps its declared listen address, exactly as a
        # deployment restart does, so the pair's configured
        # tracking wiring still names it. Its conditional startup
        # claim must meet the re-granted claim — reading as the
        # live incumbent's unyielded hold again — refuse the
        # start, and exit, rather than preempting the failover
        # successor mid-run.
        duty_listen = rig.duty_url.removeprefix("http://")
        restartee_file_floor = len(
            pair.journal_records(rig.duty_files["journal_file"])
        )
        pair.stop(rig.duty)
        rig.duty, restartee_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            None,
            rig.duty_files,
            listen=duty_listen,
            pair_token=pair.PAIR_TOKEN,
        )
        evidence["restartee_preamble"] = preamble[-4:]
        restart = {
            "served": restartee_url is not None,
            "refused": any(
                REFUSAL_WORDING in line for line in preamble
            ),
        }
        if restartee_url is None:
            if rig.duty.poll() in (None, 0):
                failures.append(
                    "the restartee is still running or exited "
                    f"cleanly (exit {rig.duty.poll()}) — its fenced "
                    "startup claim never refused it"
                )
                raise Abort
            if not restart["refused"]:
                failures.append(
                    "the restartee exited without naming the live "
                    "incumbent's refusal — the stale-resume claim "
                    "met something other than the re-armed claim: "
                    f"{preamble[-3:] or ['no diagnostic']}"
                )
                raise Abort
        digest_entries.append({"phase": "restart", **restart})

        # Phase 5 — the restartee window: through driven scans on
        # the re-armed owner, every round keeps the incumbent
        # `active`, its writes landing, the fencing verdict naming
        # its token — and the restartee, wherever its monitor
        # answers, never reporting active or promoting.
        window = []
        for round_no in range(WINDOW_ROUNDS):
            owner = pair.scan(standby_url, failures)
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            if report.get("role") != "active":
                failures.append(
                    "the live failover successor left active "
                    "through the restartee's claim — the yielded "
                    f"mark outlived the re-grant: {report}"
                )
                raise Abort
            probe = verdict_io.request({"op": "step", "dt": 0})
            if not fenced_naming(probe, armed_token):
                failures.append(
                    "the standing claim moved off the re-granted "
                    "incumbent through the restartee's claim — a "
                    f"third-party probe answered {probe}"
                )
                raise Abort
            held = failover.field_read(plant_io, cmd, failures)[
                "value"
            ]
            if held != simulate.snapshot_point(owner, cmd):
                failures.append(
                    "the re-armed incumbent's writes stopped "
                    f"landing — the field carries {held} while "
                    f"its image reports "
                    f"{simulate.snapshot_point(owner, cmd)}"
                )
                raise Abort
            row = {
                "round": round_no,
                "armed": "active",
                "probe": "fenced",
                "field_moved": False,
            }
            if restartee_url is not None:
                restartee_role = try_role(restartee_url)
                row["restartee"] = (restartee_role or {}).get("role")
                if row["restartee"] in ("active", "promoting"):
                    failures.append(
                        "the cold-restarted peer's startup claim "
                        "preempted the live successor and reached "
                        f"{row['restartee']} — the stale-resume "
                        "seizure the conditional grant exists to "
                        "refuse"
                    )
                    raise Abort
            else:
                row["restartee"] = "exited"
            window.append(row)
        digest_entries.append({"phase": "window", "window": window})

        # Phase 6 — the contender probe: a different owner's
        # conditional claim must still meet the re-armed
        # incumbent's refusal naming it — the contract's own
        # verdict the doctored case flips.
        contender = foreign_io.request(
            {"op": "claim_writer_unless_held", "owner": FOREIGN_OWNER}
        )
        evidence["contender"] = contender
        if claim_reclaim.unsupported_verb(contender):
            raise Inconclusive(
                "claim_writer_unless_held answered invalid_request "
                "— the pinned release predates the conditional "
                f"grant: {contender}"
            )
        granted = contender.get("result") in ("done", "claimed_shared")
        if granted:
            # The induction grant stands: hand its hold straight
            # back so the restore and the owner's claim are
            # untouched by it.
            try:
                foreign_io.request({"op": "release_writer"})
            except Exception:
                pass
        if tamper == "expect-granted":
            if not granted:
                failures.append(
                    "the doctored expectation wanted the foreign "
                    "claim granted — the re-armed incumbent fenced "
                    f"the conditional probe: {contender}"
                )
        else:
            if granted:
                failures.append(
                    "a different owner's conditional claim "
                    "preempted the re-granted live incumbent — the "
                    f"yielded mark outlived the re-grant: {contender}"
                )
            elif not claim_reclaim.mutation_fenced(contender):
                failures.append(
                    "the post-restart conditional probe answered "
                    f"{contender} — neither grant nor the fencing "
                    "refusal the contract defines"
                )
            elif claim_reclaim.verdict_owner(contender) != armed_token:
                failures.append(
                    "the contender's fencing verdict attributes "
                    "the claim to "
                    f"{claim_reclaim.verdict_owner(contender)}, not "
                    f"the re-granted successor: {contender}"
                )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "contender",
                "verdict": "granted" if granted else "fenced",
                "owner": owner_word(
                    claim_reclaim.verdict_owner(contender), armed_token
                ),
            }
        )

        # Phase 7 — the journal audits: the re-promoted peer's
        # served and durable records carry no `field_claim_lost`
        # and no `origin: "fenced"` demotion through the restartee
        # window — the preempted demotion a stale-resume seizure
        # would journal — and the restartee's durable journal
        # gained no run records at all.
        armed_added = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )[armed_floor:]
        armed_losses = stranded_rejoin.lost_entries(armed_added)
        armed_walk = stranded_rejoin.role_walk(armed_added)
        if any(origin is None for _frm, _to, origin in armed_walk):
            raise Inconclusive(
                "the served journal's role changes carry no origin "
                "attribution — the pinned release predates the "
                "walk fields the fencing-demotion audit reads: "
                f"{armed_walk}"
            )
        armed_fenced = [
            entry for entry in armed_walk if entry[2] == "fenced"
        ]
        if armed_losses or armed_fenced:
            failures.append(
                "the re-armed incumbent journaled a fencing loss "
                "or demotion through the restartee's claim — its "
                f"live claim was preempted: losses {armed_losses[:2]} "
                f"walk {armed_fenced[:2]}"
            )
        if armed_walk[-1:] != [("promoting", "active", "failover")]:
            failures.append(
                "the re-armed incumbent's role walk did not end on "
                "the failover re-promotion — an episode transition "
                f"outlived it: {armed_walk}"
            )
        armed_file_added = journal_entries(
            rig.standby_files["journal_file"]
        )[armed_file_floor:]
        file_losses = stranded_rejoin.lost_entries(armed_file_added)
        file_walk = stranded_rejoin.role_walk(armed_file_added)
        file_fenced = [
            entry for entry in file_walk if entry[2] == "fenced"
        ]
        if file_losses or file_fenced:
            failures.append(
                "the re-armed incumbent's durable journal carries "
                "a fencing loss or demotion the served journal "
                f"does not show: losses {file_losses[:2]} "
                f"walk {file_fenced[:2]}"
            )
        restartee_added = pair.journal_records(
            rig.duty_files["journal_file"]
        )[restartee_file_floor:]
        restartee_entries = [
            record
            for kind, record in restartee_added
            if kind == "entry"
            and "run_boundary" not in record.get("event", {})
        ]
        # The fenced start's own recorded evidence — the named
        # refusal verdict, the observed incumbent's attribution, the
        # pre-claim `restart_consult` the launch runs against the
        # incumbent's checkpoint stream, and the active -> standby
        # stand-down — is the launch's journaled verdict, not the
        # field seizure the refusal exists to stop; only records past
        # it mean the claim reached farther than it allows.
        restartee_overreach = [
            record for record in restartee_entries
            if "field_claim_observed" not in record.get("event", {})
            and "startup_claim_refused" not in record.get("event", {})
            and "restart_consult" not in record.get("event", {})
            and (record.get("event", {}).get("role_changed", {})
                 .get("to") != "standby")
        ]
        if restartee_overreach:
            failures.append(
                "the restartee's durable journal gained run "
                "records through the fenced start — its claim "
                f"reached farther than the refusal allows: "
                f"{restartee_overreach[:2]}"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "armed_losses": len(armed_losses),
                "armed_walk": armed_walk,
                "restartee_entries": len(restartee_overreach),
            }
        )

        # Phase 8 — the restore: the ex-owner rejoins as
        # `--standby` on its declared address — the documented
        # remedy the refusal names — reconverges to `tracking` on
        # the re-armed owner, and the documented switch seats it
        # back as the launch field owner, the demoted peer's
        # configured tracking source resolving the moment the
        # relaunched service answers it.
        rig.duty, duty_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            standby_url.removeprefix("http://"),
            rig.duty_files,
            listen=duty_listen,
            pair_token=pair.PAIR_TOKEN,
        )
        rig.duty_url = duty_url
        if duty_url is None:
            failures.append(
                "the rejoined standby exited at startup: "
                f"{'; '.join(preamble[-2:]) or 'no diagnostic'}"
            )
            raise Abort
        rejoin = []
        tracked = None
        for _ in range(RESTORE_SCANS):
            pair.scan(standby_url, failures)
            pair.scan(duty_url, failures)
            report = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            rejoin.append(sync_kind(report))
            if claim_reclaim.tracking(report):
                tracked = report
                break
        evidence["rejoin"] = rejoin
        if tracked is None:
            failures.append(
                "the rejoined standby never reported tracking on "
                f"the re-armed owner — its sync stayed {rejoin}"
            )
            raise Abort
        rig.demote(standby_url, failures, "the re-armed owner")
        rig.promote(duty_url, failures, "the rejoined duty member")
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
                "the launch layout never restored — the pair's "
                f"role reports stayed {restored}"
            )
            raise Abort
        post = verdict_io.request({"op": "step", "dt": 0})
        evidence["post_probe"] = post
        if not claim_reclaim.mutation_fenced(post):
            failures.append(
                "the restored claim does not fence third-party "
                f"mutations: {post}"
            )
            raise Abort
        declared_monitor = stranded_rejoin.verdict_monitor(post)
        if declared_monitor is not None:
            if not str(declared_monitor).endswith(
                ":" + stranded_rejoin.monitor_port(duty_url)
            ):
                failures.append(
                    "the restored claim declares monitor "
                    f"{declared_monitor}, not the rejoined duty "
                    f"member on "
                    f":{stranded_rejoin.monitor_port(duty_url)}"
                )
                raise Abort
            seated = "named"
        else:
            seated = (
                "owner"
                if claim_reclaim.verdict_owner(post)
                not in (None, armed_token, FOREIGN_OWNER)
                else "foreign"
            )
            if seated != "owner":
                failures.append(
                    "the restored claim names "
                    f"{claim_reclaim.verdict_owner(post)} — never "
                    f"re-seated under the rejoined duty member: {post}"
                )
                raise Abort
        digest_entries.append(
            {
                "phase": "restore",
                "rejoin": rejoin,
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
            # Best effort: the launch role layout for the legs
            # behind this one — a clean pass already restored it.
            # A dead ex-owner relaunches as the standby the refusal
            # names; a peer still owning the field demotes back to
            # the launch member once the rejoined peer tracks it.
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
        choices=["expect-granted"],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the honest fenced verdict",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = yielded_claim_rearm_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "yielded-claim-rearm: the doctored expectation wanted the foreign claim granted — an inconclusive run offers the doctored case no evidence"
            )
            return 1
        eprint(f"yielded-claim-rearm: inconclusive — {inconclusive}")
        print(f"yielded-claim-rearm-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"yielded-claim-rearm: {line}")
        return 1
    for failure in failures:
        eprint(f"yielded-claim-rearm: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"yielded-claim-rearm: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"yielded-claim-rearm-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the yielded claim's same-owner "
        "re-grant re-armed the incumbent refusal, the restartee's "
        "conditional startup claim fenced and exited, and the "
        "launch roles restored"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
