#!/usr/bin/env python3
"""The pair contract's dead-owner fencing leg — the consumer
boundary's mirror of the qa rig's dead-owner leg at
qa_lane/scenarios/2487_dead_owner_fencing.py, proving on the released
images that the plant write claim a dead owner leaves behind keeps
fencing the field (WW-ENG-003, WW-OPS-003).

The claim lifecycle's other legs each pin one door out of a claim: the
startup-claim leg proves a doomed startup cannot strand a foreign
claim over the incumbent, the claim-reclaim leg proves a *live* holder
can hand the field back and the loss-marked ex-owner re-seats it, and
the holderless-claim-recovery leg proves the fenced peer's bound
reclaim closes the holderless wedge unattended. None of them pins the
never-released rule itself — that the only door out of a dead owner's
claim is a claim verb. #638's fix settles the server half (a release
that removed no hold can no longer empty the holder set); this leg is
the consumer-boundary evidence for it.

ci/check.sh runs this script twice against the same declared
deployment as ci/legs/pair.py — `dcs-plant-server` serving the emitted
model and dynamics, plus the two manifest-declared `dcs-controller
--driven --remote` peers. A dedicated PlantClient takes the field's
write claim through the raw sim-net protocol (the claim ops the
shipped `dcs-plant-ctl` does not expose; its `read` rides the
harness's own plant client), and the leg runs:

- the baseline: with the pair converged, a third-party mutation probe
  is fenced naming the launch owner's token, which a same-token
  `ensure_writer` answers `claimed_shared`;
- the induction: a dedicated attachment takes the claim with an
  unconditional `claim_writer` under a fresh tool token and lands a
  write, so the claim that dies below is a used ownership epoch;
- the dead owner: that attachment's connection is closed *without* a
  release — the finding's own reproduction. The reap of the closed
  connection's hold is unobservable from outside (a same-token probe
  would join the holder set), so the leg reads the window the only
  honest way, by repeating the sequence itself across the reap:
- the standing fence: on every round an attachment holding nothing
  sends `release_writer` and reads the field back. The release must
  answer `done` and the claim must still stand — the probe naming the
  dead owner's token, the mutation probe `fenced`, the dead owner's own
  token still the recorded owner. A round answering `unclaimed`, or
  naming another token, is the defect: an attachment holding nothing
  dissolved the standing fence and opened the field to any next
  claimant;
- the foreign grant refused inside the window, the recorded owner's
  re-attach `ensure_writer` answering `claimed`/`claimed_shared` with
  its writes landing, the foreign token fenced again afterwards, and a
  preempting `claim_writer` from a third attachment taking the field
  naming its own token — the two documented doors out of a dead
  owner's claim, both exercised;
- the restore: the launch owner's token re-arms the field and the
  pair's roles never moved, so the legs behind this one find their
  launch state.

The leg reports inconclusive where the pinned release predates the
contract: no owner-token claim line in the launched active's preamble,
claim verbs answered `invalid_request`, no `probe_writer`-shaped
attributed fencing verdict, or no written field output to land the
induction write on — never a product failure.

Usage:

    dead_owner_fencing.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `dead-owner-fencing-digest <sha256>` line prints — the
check runs two passes and compares them
(`dead-owner-fencing-nondeterministic`). A contract violation reports
`dead-owner-fencing: …` lines on stderr and exits 1 — the check's
`dead-owner-fencing-failed`. `--tamper expect-dissolved` doctors the
leg's window expectation — asserting the field answers `unclaimed`
inside the dead-owner window — so the leg proves its standing-fence
assertion fires rather than passing an unexercised contract.
"""

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import claim_reclaim
import failover
import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored case. The
# doctored case: a leg asserting the field opens inside the dead-owner
# window must surface the named diagnostic on the honest standing
# fence — never a silently unexercised pass.
LEG = {
    "order": 382,
    "title": "the dead-owner fencing leg",
    "passes": "dead-owner-fencing",
    "tampers": [
        {
            "name": "expect-dissolved",
            "passed": "a doctored dissolved-claim expectation passed the dead-owner-fencing leg",
            "missed": "the expect-dissolved case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted the dead owner's claim dissolved"],
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


# The dead-owner window's shape: how many times the leg sends the
# non-holder release/probe sequence. The window has to span the
# server-side reap of the closed connection's hold, so it is opened by
# repetition rather than by a sleep the reap's timing would decide.
WINDOW_ROUNDS = 6

# The restore's hand-back bound. The plant reaps a closed attachment's
# hold on the server's own schedule — the never-released rule keeps the
# claim, only the hold goes — so freeing the field means waiting the
# reap out, polling the claim's own verdict rather than guessing at it.
RESTORE_SECONDS = 5.0
RESTORE_POLL = 0.2

# The induction attachment's tool owner token — a fixed value that
# cannot collide with a controller's minted token, distinct from the
# tokens the other legs stage.
DEAD_OWNER = 0xDEAD01
# The preemptor's tool owner token — the third attachment that takes
# the field back unconditionally at the end of the pass.
PREEMPT_OWNER = 0xDEAD02
# The foreign token a second attachment offers the field while the
# dead owner's claim stands — the grant the window must refuse.
FOREIGN_OWNER = 0xDEAD03


def unsupported_verb(verdict):
    """Whether a claim-verb answer is the wire's `invalid_request` —
    a release whose plant server predates the lifecycle verbs."""
    return claim_reclaim.unsupported_verb(verdict)


def dead_owner_pass(args, tamper):
    """The dead-owner run: converge, gate the claim surface, take the
    claim as a dedicated attachment, kill that attachment without a
    release, read the standing fence across the reap window, prove the
    re-arm and the preempt, and restore. Returns `(digest_entries,
    evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the dead-owner "
            "leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = verdict_io = dead_io = foreign_io = ensure_io = None
    owner_url = tracker_url = None
    released = False
    try:
        rig = pair.launch_pair(args, declared)
        owner_url, tracker_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io
        # Three dedicated attachments on the plant socket:
        # `verdict_io` runs the third-party mutation probes and never
        # holds the claim, `dead_io` is the attachment whose death is
        # the leg's subject, and `foreign_io`/`ensure_io` speak the
        # conditional verb under the foreign and recorded tokens.
        verdict_io = simulate.PlantClient(rig.plant_addr)
        dead_io = simulate.PlantClient(rig.plant_addr)
        foreign_io = simulate.PlantClient(rig.plant_addr)
        ensure_io = simulate.PlantClient(rig.plant_addr)
        with open(args.model) as handle:
            model = json.load(handle)
        points = failover.signal_points(model)
        if points is None:
            raise Abort(
                "the emitted model declares no p101-cmd/level-primary "
                "signal points — the leg has no field output to write"
            )
        cmd = points["cmd"]

        # Phase 1 — convergence on the released images. The contract
        # needs the claim lifecycle surface: an owner-token claim line
        # recorded at launch — a release that claims only on promotion
        # predates the seam.
        converged = rig.converge(failures)
        owner = converged["owner"]
        owner_token = failover.owner_token(rig.duty_preamble)
        if owner_token is None:
            raise Inconclusive(
                "the launched active recorded no owner-token claim "
                "line — the pinned release claims only on promotion, "
                "predating the claim lifecycle the dead-owner contract "
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
        # third-party mutations and attributes the launch owner. A
        # verdict naming nobody and an unspoken verb are the
        # pre-contract surface; a verdict naming a foreign token is a
        # rig not in its launch claim state.
        probe0 = verdict_io.request({"op": "step", "dt": 0})
        evidence["probe0"] = probe0
        if not claim_reclaim.mutation_fenced(probe0):
            failures.append(
                "the field held no writer claim after convergence — a "
                f"third-party probe answered {probe0}, so the leg has "
                "no standing claim to lose"
            )
            raise Abort
        if claim_reclaim.verdict_owner(probe0) is None:
            raise Inconclusive(
                "the fencing verdict names no standing owner — the "
                "pinned release predates the loss-attribution "
                f"contract: {probe0}"
            )
        if claim_reclaim.verdict_owner(probe0) != owner_token:
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
                f"dead-owner window's re-arm speaks: {ensure0}"
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
        digest_entries.append(
            {
                "phase": "baseline",
                "tick": owner["tick"],
                "probe": "fenced",
                "owner_named": "owner",
                "ensure": ensure0.get("result"),
                "field": held0,
            }
        )

        # Phase 3 — the induction: the dedicated attachment takes the
        # claim unconditionally and writes on it, so the claim that
        # dies below is a used ownership epoch.
        claim = dead_io.request(
            {"op": "claim_writer", "owner": DEAD_OWNER}
        )
        evidence["claim"] = claim
        if unsupported_verb(claim):
            raise Inconclusive(
                "claim_writer answered invalid_request — the pinned "
                f"release predates the claim contract: {claim}"
            )
        if claim.get("result") != "done":
            failures.append(
                f"the induction claim was refused: {claim} — "
                "claim_writer must preempt unconditionally"
            )
            raise Abort
        landed = dead_io.request(
            {"op": "write", "point": cmd, "value": {"bool": False}}
        )
        evidence["induction_write"] = landed
        if not claim_reclaim.mutation_fenced(landed) and (
            landed.get("result") != "done"
        ):
            failures.append(
                f"the dead owner's induction write was refused: "
                f"{landed}"
            )
            raise Abort
        seized = verdict_io.request({"op": "step", "dt": 0})
        evidence["seized"] = seized
        if not claim_reclaim.mutation_fenced(seized) or (
            claim_reclaim.verdict_owner(seized) != DEAD_OWNER
        ):
            failures.append(
                "the induction claim does not fence third-party "
                f"mutations naming its own token: {seized}"
            )
            raise Abort
        digest_entries.append(
            {"phase": "induction", "claim": "done", "seized": "dead"}
        )

        # Phase 4 — the dead owner: close the attachment without a
        # release. This is the finding's own reproduction — a claim
        # whose owner is gone, never handed back. The claim outlives
        # the connection by contract; only the hold is reaped, on the
        # server's own schedule.
        dead_io.close()
        dead_io = None

        # Phase 5 — the standing fence across the reap window. The
        # empty holder set is unobservable — a same-token probe would
        # join it — so the leg sends the non-holder sequence itself,
        # round after round, and judges every round. `foreign_io` holds
        # nothing throughout: its releases are the non-holder releases
        # the finding reproduces.
        window = []
        for index in range(WINDOW_ROUNDS):
            probe = verdict_io.request({"op": "step", "dt": 0})
            mutation = foreign_io.request({"op": "step", "dt": 0})
            release = foreign_io.request({"op": "release_writer"})
            window.append(
                {
                    "round": index,
                    "probe": (
                        "fenced"
                        if claim_reclaim.mutation_fenced(probe)
                        else "open"
                    ),
                    "named": (
                        "dead"
                        if claim_reclaim.verdict_owner(probe)
                        == DEAD_OWNER
                        else "other"
                    ),
                    "mutation": (
                        "fenced"
                        if claim_reclaim.mutation_fenced(mutation)
                        else "open"
                    ),
                    "release": release.get("result"),
                }
            )
        evidence["window"] = window
        for row in window:
            label = f"dead-owner window round {row['round']}"
            standing = (
                row["probe"] == "fenced" and row["named"] == "dead"
            )
            if tamper == "expect-dissolved":
                # The doctored expectation: the claim dissolves
                # somewhere in the window. The honest run keeps it
                # standing on every round, so the doctored case finds
                # no evidence for what it wants and must name that.
                if standing:
                    failures.append(
                        f"{label}: the doctored expectation wanted the "
                        "dead owner's claim dissolved — the claim "
                        f"stands naming the dead owner ({row})"
                    )
            elif not standing:
                failures.append(
                    f"{label}: the standing claim no longer names the "
                    "dead owner — a release from an attachment holding "
                    f"nothing dissolved the dead owner's fence: {row}"
                )
            if row["release"] != "done":
                failures.append(
                    f"{label}: a non-holder's release_writer answered "
                    f"{row['release']} — the settled contract answers a "
                    "release that removed no hold `done`, changing "
                    "nothing"
                )
            if row["mutation"] != "fenced":
                failures.append(
                    f"{label}: the dead owner's claim did not fence a "
                    f"mutation probe from an attachment holding "
                    f"nothing: {row}"
                )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "window",
                "rounds": [
                    {
                        "probe": row["probe"],
                        "named": row["named"],
                        "mutation": row["mutation"],
                        "release": row["release"],
                    }
                    for row in window
                ],
            }
        )

        # Phase 6 — the foreign grant refused inside the window, and
        # the recorded owner's re-arm: its own token returns, its
        # write lands, and the foreign token is fenced again after.
        refused = foreign_io.request(
            {"op": "ensure_writer", "owner": FOREIGN_OWNER}
        )
        evidence["foreign_ensure"] = refused
        if not claim_reclaim.mutation_fenced(refused):
            failures.append(
                "a foreign token's ensure_writer was granted while "
                f"the dead owner's claim stood: {refused}"
            )
            raise Abort
        rearmer = simulate.PlantClient(rig.plant_addr)
        rearm = rearmer.request(
            {"op": "ensure_writer", "owner": DEAD_OWNER}
        )
        evidence["rearm"] = rearm
        if rearm.get("result") not in ("done", "claimed_shared"):
            failures.append(
                "the recorded owner's re-attach ensure_writer "
                f"answered {rearm} — a dead owner's own token must "
                "re-arm against its standing claim"
            )
            raise Abort
        rewrite = rearmer.request(
            {"op": "write", "point": cmd, "value": {"bool": False}}
        )
        evidence["rearm_write"] = rewrite
        if rewrite.get("result") != "done":
            failures.append(
                f"the re-armed owner's write was fenced: {rewrite}"
            )
            raise Abort
        after = foreign_io.request(
            {"op": "ensure_writer", "owner": FOREIGN_OWNER}
        )
        evidence["foreign_after"] = after
        if not claim_reclaim.mutation_fenced(after):
            failures.append(
                "a foreign token's ensure_writer was granted after "
                "the recorded owner re-armed — the claim passed to "
                f"another token: {after}"
            )
            raise Abort
        rearmer.close()
        digest_entries.append(
            {
                "phase": "rearm",
                "foreign": "refused",
                "rearm": rearm.get("result"),
                "write": "landed",
            }
        )

        # Phase 7 — the preempt: the only other door out of a dead
        # owner's claim. `claim_writer` preempts unconditionally, so
        # the field moves to the third attachment's token and the
        # recorded owner's mutations are fenced behind it.
        preempt = simulate.PlantClient(rig.plant_addr)
        took = preempt.request(
            {"op": "claim_writer", "owner": PREEMPT_OWNER}
        )
        evidence["preempt"] = took
        standing = verdict_io.request({"op": "step", "dt": 0})
        evidence["preempted"] = standing
        preempt.close()
        released = True
        if took.get("result") not in ("done", "claimed_shared"):
            failures.append(
                f"the preempting claim_writer was refused: {took} — "
                "claim_writer preempts a standing dead owner "
                "unconditionally"
            )
            raise Abort
        if claim_reclaim.verdict_owner(standing) != PREEMPT_OWNER:
            failures.append(
                "the standing claim after the preempt names "
                f"{claim_reclaim.verdict_owner(standing)}, not the "
                f"preemptor's token {PREEMPT_OWNER:#x}: {standing}"
            )
            raise Abort
        digest_entries.append(
            {"phase": "preempt", "preempt": took.get("result")}
        )

        # Phase 8 — the restore: the launch owner's token takes the
        # field back and the pair's roles never moved.
        #
        # The restore cannot wait for an `unclaimed` window: the duty
        # peer is live and re-asserts its own claim on every scan, so
        # the field is barely ever free. What the restore does instead
        # is the same-owner `ensure_writer` against a claim the pass's
        # own staging token still holds — which preempts nothing and
        # abandons no live incumbent — after handing that staging claim
        # back so the re-arm starts from a claim no closed attachment
        # is holding. The hand-back takes the corpse's reaping into
        # account: a release under the preemptor's token joins and
        # drops only the fixer's own hold, so it takes effect once the
        # plant reaps that corpse on its own schedule. Polling the
        # verdict to a bound is the honest wait; a fixed round count
        # would race that reap and read a standing claim as a wedged
        # restore.
        fixer = simulate.PlantClient(rig.plant_addr)
        try:
            staged = claim_reclaim.verdict_owner(
                fixer.request({"op": "step", "dt": 0})
            )
            if staged == PREEMPT_OWNER:
                deadline = time.monotonic() + RESTORE_SECONDS
                while (
                    staged == PREEMPT_OWNER
                    and time.monotonic() < deadline
                ):
                    fixer.request(
                        {"op": "claim_writer", "owner": PREEMPT_OWNER}
                    )
                    hand_back = fixer.request(
                        {"op": "release_writer"}
                    )
                    if hand_back.get("result") != "done":
                        failures.append(
                            "the restore's hand-back was refused: "
                            f"{hand_back}"
                        )
                        raise Abort
                    time.sleep(RESTORE_POLL)
                    staged = claim_reclaim.verdict_owner(
                        fixer.request({"op": "step", "dt": 0})
                    )
                if staged == PREEMPT_OWNER:
                    failures.append(
                        "the restore never handed the staging claim "
                        "back: the plant still reports it after "
                        f"{RESTORE_SECONDS:g}s of hand-backs"
                    )
                    raise Abort
            restored = fixer.request(
                {"op": "ensure_writer", "owner": owner_token}
            )
            if restored.get("result") not in (
                "done",
                "claimed_shared",
            ):
                failures.append(
                    "the restore did not re-arm the launch owner's "
                    f"claim: {restored}"
                )
                raise Abort
            final = verdict_io.request({"op": "step", "dt": 0})
        finally:
            fixer.close()
        evidence["restored"] = final
        if claim_reclaim.verdict_owner(final) != owner_token:
            failures.append(
                "the restore left the field under "
                f"{claim_reclaim.verdict_owner(final)}, not the launch "
                f"owner's token {owner_token:#x}: {final}"
            )
            raise Abort
        duty_role = pair.get(
            f"{owner_url}/role", "GET /role", failures
        )
        tracker_role = pair.get(
            f"{tracker_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active":
            failures.append(
                "the restore moved the pair's field owner off active "
                f"— GET /role answers {duty_role}"
            )
            raise Abort
        if not claim_reclaim.tracking(tracker_role):
            failures.append(
                "the restore moved the tracking peer off tracking "
                f"standby — GET /role answers {tracker_role}"
            )
            raise Abort
        held = failover.field_read(plant_io, cmd, failures)["value"]
        digest_entries.append(
            {
                "phase": "restore",
                "owner_named": "owner",
                "duty_role": duty_role,
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
            # launch owner's re-arm. Re-take the staging token through
            # a fresh attachment and release it — only while the
            # verdict still names that token, so a finished restore is
            # never re-preempted.
            if dead_io is not None:
                try:
                    dead_io.request({"op": "release_writer"})
                except Exception:
                    pass
            try:
                fixer = simulate.PlantClient(rig.plant_addr)
                try:
                    # Repeated for the same reason the pass's own
                    # restore is: a staging claim outlives its closed
                    # attachment by the never-released rule, so one
                    # hand-back can leave a reaped-but-standing corpse
                    # holding the token. Each round drops one more.
                    deadline = time.monotonic() + RESTORE_SECONDS
                    while time.monotonic() < deadline:
                        probe = fixer.request({"op": "step", "dt": 0})
                        if not claim_reclaim.mutation_fenced(probe):
                            break
                        if claim_reclaim.verdict_owner(probe) not in (
                            DEAD_OWNER,
                            PREEMPT_OWNER,
                        ):
                            # A live claim the pass never staged — the
                            # launch owner's own. Never re-preempted.
                            break
                        fixer.request(
                            {"op": "claim_writer", "owner": DEAD_OWNER}
                        )
                        fixer.request({"op": "release_writer"})
                        time.sleep(RESTORE_POLL)
                finally:
                    fixer.close()
            except Exception:
                pass  # an unreachable plant is the pass's own verdict
            try:
                restore = simulate.PlantClient(rig.plant_addr)
                try:
                    restore.request(
                        {"op": "ensure_writer", "owner": owner_token}
                    )
                finally:
                    restore.close()
            except Exception:
                pass
        if rig is not None:
            rig.close()
        for client in (verdict_io, dead_io, foreign_io, ensure_io):
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
        choices=["expect-dissolved"],
        help="doctor the leg's own window expectation — the pass "
        "must fail naming the honest standing fence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = dead_owner_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "dead-owner-fencing: the doctored expectation wanted "
                "the dead owner's claim dissolved — an inconclusive run "
                "offers the doctored case no evidence"
            )
            return 1
        eprint(f"dead-owner-fencing: inconclusive — {inconclusive}")
        print(
            f"dead-owner-fencing-digest inconclusive — {inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"dead-owner-fencing: {line}")
        return 1
    for failure in failures:
        eprint(f"dead-owner-fencing: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"dead-owner-fencing: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"dead-owner-fencing-digest {digest} — tracking by tick "
        f"{len(evidence.get('window', []))} reap-window rounds, the "
        "dead owner's claim kept fencing every third attachment's mutation "
        "across the server-side reap of its hold, a release from an "
        "attachment holding nothing answered done and changed nothing, "
        "the recorded owner's token re-armed and wrote, and a "
        "preempting claim took the field per the declared contract"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())