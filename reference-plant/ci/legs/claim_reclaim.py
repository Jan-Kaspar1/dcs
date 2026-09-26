#!/usr/bin/env python3
"""The claim-reclaim leg for the reference plant — the consumer-side
proof of the released-preemption fencing-loss reclaim contract
(WW-ENG-003, WW-LCM-001): when a foreign claimer preempts the field's
single-writer claim and then releases it, the superseded peer is
demoted by its own fenced write, the loss is journaled naming the
claimant, and the released claim re-arms to the standing owner —
unattended, with the pair reconverging to its launch roles.

The startup-claim leg (`ci/legs/startup_claim.py`) proves a doomed
startup cannot strand a foreign claim over the incumbent, and the pair
leg proves convergence and the receipted switch. This leg exercises
the remaining half of the claim lifecycle: a foreign attachment that
preempts the live claim on purpose and hands it back. With the pair
settled on the released images, a dedicated plant-socket attachment —
the same sim-net claim protocol the startup-claim leg's tamper drives
— stages the episode:

- `claim_writer` under a foreign owner token preempts the standing
  claim unconditionally; the field's fencing verdict for third-party
  mutations now names the induction token;
- the field owner's next scan fences its write and demotes it in
  place — its monitor keeps answering, because a degrade is not a
  death — and its served journal gains `field_claim_lost` attributed
  to the foreign token, beside the `active → demoting → standby`
  role walk;
- while the foreign claim stands, the marked ex-owner's bound
  conditional re-grant refuses to take a different-owner claim — it
  stays standby, third-party probes stay fenced naming the induction
  token, and the field records no foreign write;
- after `release_writer`, the loss-marked re-grant re-seats the field
  under the launch owner's own token — the claim re-arms to the
  standing owner rather than staying foreign or lapsing unowned — the
  ex-owner walks `standby → promoting → active` with no operator call
  and no restart, its writes land again, and the pair reconverges to
  one active plus one tracking standby with the launch roles
  restored.

The leg reports inconclusive where the pinned release predates the
contract: no owner-token claim line in the launched active's preamble
(the claim-on-promotion release line), claim verbs answered
`invalid_request`, or a fencing verdict that names no standing owner.

Usage:

    claim_reclaim.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `claim-reclaim-digest <sha256>` line prints — the check
runs two passes and compares them (`claim-reclaim-nondeterministic`).
A contract violation reports `claim-reclaim: …` lines on stderr and
exits 1 — the check's `claim-reclaim-failed`. `--tamper expect-foreign`
doctors the leg's re-arm expectation — asserting the released claim
re-arms to the foreign claimant — so the leg proves its
standing-owner assertion fires rather than passing an unexercised
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

import failover
import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the released claim re-arms to
# the foreign claimant must surface the named diagnostic on the
# honest standing-owner re-arm — never a silently unexercised pass.
LEG = {
    "order": 370,
    "title": "the claim-reclaim leg",
    "passes": "claim-reclaim",
    "tampers": [
        {
            "name": "expect-foreign",
            "passed": "a doctored foreign-owner expectation passed the claim-reclaim leg",
            "missed": "the expect-foreign case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted the claim foreign-owned"],
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


# The driven-scan bounds. The fencing loss demotes at the first fenced
# write and settles standby on the next quiesced scan; the bound
# conditional re-grant lands on the first scan the field stands
# unclaimed. The bounds are a handful of driven scans, mirroring the
# claim-fencing stage's watch and reconvergence windows.
WATCH_SCANS = 4
RECONVERGE_SCANS = 6
HOLD_SCANS = 3

# The induction attachment's foreign owner token — a small fixed value
# that cannot collide with a controller's per-process minted token,
# distinct from the tokens the other legs stage.
FOREIGN_OWNER = 0xF017


def mutation_fenced(verdict):
    """Whether a field-mutation answer is the named fencing refusal —
    `write` carries the point's io-fenced error nested under the `io`
    kind; `step` and the claim verbs carry the plant-level `fenced`
    kind."""
    error = (verdict or {}).get("error") or {}
    if error.get("kind") == "fenced":
        return True
    inner = error.get("error")
    return (
        error.get("kind") == "io"
        and isinstance(inner, dict)
        and "fenced" in inner
    )


def verdict_owner(verdict):
    """The owner token a fencing verdict attributes the standing claim
    to — the `owner` field the plant's `fenced` and `io.fenced`
    answers both carry under the loss-attribution contract — or None
    on an unfenced answer or a build predating the field."""
    return ((verdict or {}).get("error") or {}).get("owner")


def unsupported_verb(verdict):
    """Whether a claim-verb answer is the wire's `invalid_request` —
    a release whose plant server predates the lifecycle verbs."""
    error = (verdict or {}).get("error") or {}
    return error.get("kind") == "invalid_request"


def sync_state(report):
    """The served RoleReport's sync-state variant name — `tracking`,
    `orphaned`, `degraded` — or None when the report carries none."""
    sync = report.get("sync") if isinstance(report, dict) else None
    return next(iter(sync)) if isinstance(sync, dict) and sync else None


def tracking(report):
    """Whether a served RoleReport carries `standby` under the
    `tracking` sync state."""
    return (
        isinstance(report, dict)
        and report.get("role") == "standby"
        and sync_state(report) == "tracking"
    )


def converged_sync(report):
    """Whether a served RoleReport carries `standby` under a converged
    sync state — `tracking`, or the `orphaned` verdict reported while
    the tracked line's serving run holds no field claim (the demoted
    owner's honest state while the foreign claim stands)."""
    return (
        isinstance(report, dict)
        and report.get("role") == "standby"
        and sync_state(report) in ("tracking", "orphaned")
    )


def try_role(url):
    """`GET /role` that answers None on transport error — for polls
    and the best-effort restore, where a missed answer is data, not a
    harness failure."""
    try:
        return simulate.http(f"{url}/role")
    except Exception:
        return None


def try_scan(url):
    """One driven `POST /scan` that answers None on transport error —
    for the restore path only; the leg's own phases use `pair.scan`
    so transport errors are recorded failures."""
    try:
        return simulate.http(f"{url}/scan", {"scans": 1})
    except Exception:
        return None


def claim_reclaim_pass(args, tamper):
    """The claim-reclaim run: converge, gate the contract surface,
    foreign preempt, fenced-write demotion, held-window refusal,
    release, bound reclaim, re-arm verification, and the pair's
    reconvergence to launch roles. Returns `(digest_entries,
    evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the claim-reclaim "
            "leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = verdict_io = foreign_io = ensure_io = None
    owner_url = tracker_url = None
    released = False
    try:
        rig = pair.launch_pair(args, declared)
        owner_url, tracker_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io
        # Three dedicated attachments on the plant socket: `verdict_io`
        # runs third-party mutation probes and never holds the claim,
        # `foreign_io` stages the preempt-and-release, and `ensure_io`
        # speaks the conditional verb under the standing owner's token.
        verdict_io = simulate.PlantClient(rig.plant_addr)
        foreign_io = simulate.PlantClient(rig.plant_addr)
        ensure_io = simulate.PlantClient(rig.plant_addr)
        with open(args.model) as handle:
            model = json.load(handle)
        points = failover.signal_points(model)
        if points is None:
            raise Abort(
                "the emitted model declares no p101-cmd/level-primary "
                "signal points — the leg has no field output to watch"
            )
        cmd = points["cmd"]

        # Phase 1 — convergence on the released images. The contract
        # this leg exercises needs the claim lifecycle surface: an
        # owner-token claim line recorded at launch — a release that
        # claims only on promotion predates the seam the loss-marked
        # re-grant runs on.
        converged = rig.converge(failures)
        owner = converged["owner"]
        owner_token = failover.owner_token(rig.duty_preamble)
        if owner_token is None:
            raise Inconclusive(
                "the launched active recorded no owner-token claim "
                "line — the pinned release claims only on promotion, "
                "predating the claim lifecycle the reclaim contract "
                "stands on"
            )
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # Phase 2 — the contract gate: a standing claim that fences
        # third-party mutations and attributes the launch owner, plus
        # the conditional verb the loss-marked re-grant speaks. A
        # verdict naming a foreign token is a failure — the rig is not
        # in its launch claim state — while a verdict naming nobody
        # and an unspoken verb are the pre-contract surface.
        probe0 = verdict_io.request({"op": "step", "dt": 0})
        evidence["probe0"] = probe0
        if not mutation_fenced(probe0):
            failures.append(
                "the field held no writer claim after convergence — a "
                f"third-party probe answered {probe0}, so the leg has "
                "no standing claim to lose"
            )
            raise Abort
        if verdict_owner(probe0) is None:
            raise Inconclusive(
                "the fencing verdict names no standing owner — the "
                "pinned release predates the loss-attribution "
                f"contract: {probe0}"
            )
        if verdict_owner(probe0) != owner_token:
            failures.append(
                "the standing claim names a foreign token, not the "
                f"launch owner — the pair is not in its launch claim "
                f"state: {probe0}"
            )
            raise Abort
        ensure0 = ensure_io.request(
            {"op": "ensure_writer", "owner": owner_token}
        )
        if unsupported_verb(ensure0):
            raise Inconclusive(
                "ensure_writer answered invalid_request — the pinned "
                "release predates the conditional-claim verbs the "
                f"bound re-grant speaks: {ensure0}"
            )
        if (
            ensure0.get("result") != "claimed_shared"
            or ensure0.get("owner") != owner_token
        ):
            failures.append(
                "ensure_writer under the standing owner's token "
                f"answered {ensure0}, expected claimed_shared"
            )
            raise Abort
        held0 = failover.field_read(plant_io, cmd, failures)["value"]
        image_cmd = simulate.snapshot_point(owner, cmd)
        if held0 != image_cmd:
            failures.append(
                f"the field carries {held0} on the served out-point "
                f"while the field owner reports {image_cmd} — the "
                "settled pair's writes never landed"
            )
            raise Abort
        journal0 = pair.get(
            f"{owner_url}/journal", "GET /journal", failures
        )
        peer_journal0 = pair.get(
            f"{tracker_url}/journal", "GET /journal", failures
        )
        digest_entries.append(
            {
                "phase": "baseline",
                "tick": owner["tick"],
                "probe": "fenced",
                "owner_named": "owner",
                "ensure": ensure0.get("result"),
                "field": held0,
                "journal_entries": len(journal0),
            }
        )

        # Phase 3 — the induction: claim_writer preempts
        # unconditionally and the attachment holds the claim through
        # the fenced-write demotion — the slow window a same-breath
        # release never opens.
        claim = foreign_io.request(
            {"op": "claim_writer", "owner": FOREIGN_OWNER}
        )
        evidence["claim"] = claim
        if unsupported_verb(claim):
            raise Inconclusive(
                "claim_writer answered invalid_request — the pinned "
                f"release predates the claim contract: {claim}"
            )
        verdict = claim.get("result")
        if verdict == "claimed_shared":
            failures.append(
                "the preempting claim joined a live foreign holder — "
                "a leaked attachment shares the induction token: "
                f"{claim}"
            )
            raise Abort
        if verdict != "done":
            failures.append(
                f"the foreign claim was refused: {claim} — "
                "claim_writer must preempt unconditionally"
            )
            raise Abort
        seized = verdict_io.request({"op": "step", "dt": 0})
        evidence["seized"] = seized
        if not mutation_fenced(seized):
            failures.append(
                "the field accepted a third-party mutation under the "
                f"preempted claim: {seized}"
            )
            raise Abort
        if verdict_owner(seized) != FOREIGN_OWNER:
            failures.append(
                "the fencing verdict attributes the preempted claim "
                f"to {verdict_owner(seized)}, not the induction "
                f"token: {seized}"
            )
            raise Abort
        digest_entries.append(
            {"phase": "induction", "claim": verdict, "seized": "foreign"}
        )

        # Phase 4 — the fenced-write demotion: the superseded owner's
        # next scan fences its write and demotes it in place — its
        # monitor answering throughout, because a degrade is not a
        # death.
        roles = []
        settled = None
        for _ in range(WATCH_SCANS):
            pair.scan(owner_url, failures)
            report = pair.get(
                f"{owner_url}/role", "GET /role", failures
            )
            roles.append(report.get("role"))
            if report.get("role") == "standby":
                settled = report
                break
        if settled is None:
            failures.append(
                "the superseded owner never demoted — the preempt "
                f"moved the claim but the role stayed {roles}"
            )
            raise Abort
        digest_entries.append({"phase": "demotion", "watch": roles})

        # Phase 5 — the journaled loss: at least one
        # `field_claim_lost` above the floor, every record attributing
        # the takeover to the induction token the field's own fencing
        # verdict named, beside the active → demoting → standby walk.
        journal1 = pair.get(
            f"{owner_url}/journal", "GET /journal", failures
        )
        added = journal1[len(journal0) :]
        losses = [
            entry["event"]["field_claim_lost"]
            for entry in added
            if "field_claim_lost" in entry.get("event", {})
        ]
        if not losses:
            failures.append(
                "the preemption was silent — the superseded owner's "
                "journal recorded no field_claim_lost"
            )
            raise Abort
        if any(
            loss.get("claimant") != FOREIGN_OWNER for loss in losses
        ):
            failures.append(
                "the field_claim_lost records do not attribute the "
                f"takeover to the induction token "
                f"{FOREIGN_OWNER:#x}: {losses}"
            )
            raise Abort
        transitions = [
            (frm, to)
            for _tick, frm, to in pair.role_transitions(added)
        ]
        if transitions != [
            ("active", "demoting"),
            ("demoting", "standby"),
        ]:
            failures.append(
                "the superseded owner's role walk is "
                f"{transitions}, expected active → demoting → "
                "standby"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "loss",
                "losses": len(losses),
                "attributed": "claimant",
                "transitions": transitions,
            }
        )

        # Phase 6 — the held window: the induction attachment keeps
        # the foreign claim while the marked ex-owner's bound
        # conditional re-grant probes every scan. Each scan must
        # refuse: the ex-owner stays demoted, the verdict keeps naming
        # the induction token, and the field records no foreign write.
        hold = []
        for _ in range(HOLD_SCANS):
            pair.scan(owner_url, failures)
            report = pair.get(
                f"{owner_url}/role", "GET /role", failures
            )
            peer_report = try_role(tracker_url)
            probe = verdict_io.request({"op": "step", "dt": 0})
            held = failover.field_read(plant_io, cmd, failures)[
                "value"
            ]
            hold.append(
                {
                    "owner": report.get("role"),
                    "owner_sync": sync_state(report),
                    "peer": (peer_report or {}).get("role"),
                    "probe_owner": (
                        "foreign"
                        if verdict_owner(probe) == FOREIGN_OWNER
                        else "other"
                    ),
                    "field_moved": held != held0,
                }
            )
            if not converged_sync(report):
                failures.append(
                    "the marked ex-owner left converged standby while "
                    "a different-owner claim stood — the bound grant "
                    f"took what it must refuse: {report}"
                )
                raise Abort
            if peer_report is None:
                failures.append(
                    "the tracking peer's monitor stopped answering "
                    "through the held window"
                )
                raise Abort
            if not converged_sync(peer_report):
                failures.append(
                    "the tracking peer left converged standby through "
                    f"the held window: {peer_report}"
                )
                raise Abort
            if not mutation_fenced(probe) or (
                verdict_owner(probe) != FOREIGN_OWNER
            ):
                failures.append(
                    "the standing claim moved off the induction "
                    "token during the hold — the bound grant "
                    "preempted the different-owner claim it must "
                    f"refuse: {probe}"
                )
                raise Abort
            if held != held0:
                failures.append(
                    "the field moved under the held foreign claim — "
                    f"a foreign write landed: {held0} -> {held}"
                )
                raise Abort
        digest_entries.append({"phase": "hold", "hold": hold})

        # Phase 7 — the release: the preemptor hands the claim back,
        # and the loss-marked ex-owner's bound conditional re-grant
        # lands the first scan the field stands unclaimed — walking
        # standby → promoting → active with no operator call and no
        # restart.
        release = foreign_io.request({"op": "release_writer"})
        evidence["release"] = release
        if unsupported_verb(release):
            raise Inconclusive(
                "release_writer answered invalid_request — the "
                "pinned release predates the claim lifecycle: "
                f"{release}"
            )
        if release.get("result") != "done":
            failures.append(
                f"the preemptor's claim hand-back was refused: "
                f"{release}"
            )
            raise Abort
        released = True
        reclaim = []
        promoted = None
        for _ in range(RECONVERGE_SCANS):
            pair.scan(owner_url, failures)
            report = pair.get(
                f"{owner_url}/role", "GET /role", failures
            )
            reclaim.append(report.get("role"))
            if report.get("role") == "active":
                promoted = report
                break
        if promoted is None:
            failures.append(
                "the released field was never re-seated — the "
                "loss-marked reclaim never ran: reported roles "
                f"{reclaim}"
            )
            raise Abort
        digest_entries.append({"phase": "reclaim", "watch": reclaim})

        # Phase 8 — the re-arm verification: a third-party probe meets
        # the fence naming the launch owner's token — the released
        # claim re-armed to the standing owner, not foreign-held and
        # not unowned — and the owner-token join still answers
        # claimed_shared.
        reseated = verdict_io.request({"op": "step", "dt": 0})
        evidence["reseated"] = reseated
        if not mutation_fenced(reseated):
            failures.append(
                "the re-seated claim does not fence third-party "
                f"mutations: {reseated}"
            )
            raise Abort
        reseated_owner = verdict_owner(reseated)
        owner_word = (
            "owner"
            if reseated_owner == owner_token
            else "foreign"
            if reseated_owner == FOREIGN_OWNER
            else "unowned"
            if reseated_owner is None
            else "other"
        )
        if tamper == "expect-foreign":
            if owner_word != "foreign":
                failures.append(
                    "the doctored expectation wanted the claim "
                    f"foreign-owned — the re-armed claim names the "
                    f"launch owner ({owner_word})"
                )
        elif owner_word != "owner":
            failures.append(
                "the released claim did not re-arm to the standing "
                f"owner — the fencing verdict names "
                f"{reseated_owner}: {reseated}"
            )
            raise Abort
        ensure1 = ensure_io.request(
            {"op": "ensure_writer", "owner": owner_token}
        )
        if ensure1.get("result") != "claimed_shared":
            if tamper != "expect-foreign":
                failures.append(
                    "the re-armed claim does not answer "
                    "claimed_shared under the standing owner's "
                    f"token: {ensure1}"
                )
                raise Abort
        elif tamper == "expect-foreign":
            failures.append(
                "the doctored expectation wanted the claim "
                "foreign-owned — the owner-token join still "
                "answered claimed_shared"
            )
        digest_entries.append(
            {
                "phase": "rearm",
                "owner": owner_word,
                "ensure": ensure1.get("result"),
            }
        )

        # Phase 9 — the reconvergence: driven tracking-first ticks
        # prove the restored pair keeps identical images, the
        # reclaimed owner settles active, the peer returns to
        # tracking, and the re-seated claim passes the owner's own
        # writes — a holderless re-take would fence them.
        handover = []
        for _ in range(pair.HANDOVER_TICKS):
            _tracked, owner = rig.tick(
                tracker_url,
                owner_url,
                failures,
                diverged="the restored pair's images diverged at "
                "tick {tick} — the reclaim was not bumpless",
            )
            handover.append(owner["tick"])
        owner_role = pair.get(
            f"{owner_url}/role", "GET /role", failures
        )
        tracker_role = pair.get(
            f"{tracker_url}/role", "GET /role", failures
        )
        if owner_role.get("role") != "active":
            failures.append(
                "the reclaimed owner never settled active — GET "
                f"/role answers {owner_role}"
            )
            raise Abort
        if not tracking(tracker_role):
            failures.append(
                "the tracking peer did not return to tracking "
                f"standby — GET /role answers {tracker_role}"
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

        # Phase 10 — the journal tail: the walk back to active is
        # journaled as ordinary role changes — the unattended
        # recovery's record — while the tracking peer's journal
        # carries no loss it never owned and no role change.
        journal2 = pair.get(
            f"{owner_url}/journal", "GET /journal", failures
        )
        walk = [
            (frm, to)
            for _tick, frm, to in pair.role_transitions(
                journal2[len(journal0) :]
            )
        ]
        if ("promoting", "active") not in walk and (
            "standby",
            "active",
        ) not in walk:
            failures.append(
                "the unattended reclaim left no journaled walk back "
                "to active — an operator or restart path ran "
                f"instead: {walk}"
            )
            raise Abort
        peer_journal = pair.get(
            f"{tracker_url}/journal", "GET /journal", failures
        )
        for entry in peer_journal[len(peer_journal0) :]:
            event = entry.get("event") or {}
            if "field_claim_lost" in event:
                failures.append(
                    "the tracking peer journaled a fencing loss it "
                    "never owned"
                )
            if "role_changed" in event:
                failures.append(
                    "the tracking peer journaled a role change "
                    "through the episode"
                )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "reconverge",
                "ticks": handover,
                "walk": walk,
                "owner_role": owner_role,
                "tracker_role": tracker_role,
                "field": held,
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if not released and rig is not None:
            # Best effort: a stranded foreign hold keeps fencing the
            # ex-owner's bound re-grant. Drop the staging attachment's
            # own hold; when that connection is already gone, re-take
            # the induction token through a fresh attachment and
            # release — only while the verdict still names it foreign,
            # so a finished reclaim is never re-preempted.
            if foreign_io is not None:
                try:
                    foreign_io.request({"op": "release_writer"})
                except Exception:
                    try:
                        fixer = simulate.PlantClient(rig.plant_addr)
                        try:
                            probe = fixer.request(
                                {"op": "step", "dt": 0}
                            )
                            if verdict_owner(probe) == FOREIGN_OWNER:
                                fixer.request(
                                    {
                                        "op": "claim_writer",
                                        "owner": FOREIGN_OWNER,
                                    }
                                )
                                fixer.request(
                                    {"op": "release_writer"}
                                )
                        finally:
                            fixer.close()
                    except Exception:
                        pass  # an unreachable plant is the pass's own
                        # verdict
        if rig is not None and owner_url is not None:
            # Best effort: the launch role layout for the legs behind
            # this one. Give a pending bound re-grant a few scans to
            # land — the freed field may already be re-seating — then
            # the documented operator promote is the fallback on a
            # wedge; a peer moved off standby demotes back. A clean
            # pass moved nothing, so neither fires.
            try:
                for _ in range(RECONVERGE_SCANS):
                    if (try_role(owner_url) or {}).get(
                        "role"
                    ) == "active":
                        break
                    try_scan(owner_url)
                if (try_role(owner_url) or {}).get("role") != "active":
                    pair.request(f"{owner_url}/promote", {})
            except Exception:
                pass
            try:
                if (try_role(tracker_url) or {}).get("role") != (
                    "standby"
                ):
                    pair.request(f"{tracker_url}/demote", {})
            except Exception:
                pass
            rig.close()
        for client in (verdict_io, foreign_io, ensure_io):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
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
        choices=["expect-foreign"],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the honest re-arm",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = claim_reclaim_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "claim-reclaim: the doctored expectation wanted the claim foreign-owned — an inconclusive run offers the doctored case no evidence"
            )
            return 1
        eprint(f"claim-reclaim: inconclusive — {inconclusive}")
        print(f"claim-reclaim-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"claim-reclaim: {line}")
        return 1
    for failure in failures:
        eprint(f"claim-reclaim: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"claim-reclaim: the {args.tamper} case passed "
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
        f"claim-reclaim-digest {digest} — tracking by tick "
        f"{evidence.get('final_tick', '?')}, the foreign preempt "
        "demoted the field owner in place with the loss attributed "
        "to the induction token, the released claim re-armed to the "
        "standing owner, and the pair reconverged to its launch "
        "roles"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
