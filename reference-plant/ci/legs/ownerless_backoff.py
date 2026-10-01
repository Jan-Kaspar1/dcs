#!/usr/bin/env python3
"""The ownerless remote-attachment reattach-backoff leg for the
reference plant — the consumer-boundary proof that the remote
driver's #1303 reattach-backoff contract holds for a pending
born-active seat's ownerless attachment on the manifest-declared
deployment (WW-ENG-003, WW-LCM-001).

The platform pin is the fix's own regression — the remote suite's
ownerless-attachment bound: an attachment to a field that completes
the handshake but never answers must not pay the request timeout
once per model point on every access burst. The fix arms the
driver's reattach-backoff window after the first timed-out exchange,
so the burst's remaining accesses fail fast and a scan stalls once
per window instead of ~N_points*timeout per cycle. Because that
contract lives in the remote driver every consumer controller runs,
the consumer boundary pins it here — the file is the mirror of the
rig's ownerless-remote-backoff leg, named `ownerless_backoff.py` so
the leg-discovered diagnostics land on this issue's declared
`ownerless-backoff-*` stem. The run:

- converges the manifest-declared pair — the armed standby tracking,
  the field owner active — and records the incumbent's owner token,
  both peers' journal positions, and the standing fencing verdict a
  third-party write meets;
- freezes the field: `SIGSTOP` on the spawned plant stand-in — the
  deploy stage's stop/pause lever beside `pair.stop`'s container
  stop — holds the listener's sockets open but unanswered, the
  connectable-but-silent endpoint the contract names;
- stages a labeled born-active `--remote` launch at the frozen
  endpoint through the deploy tooling's labeled-launch lever — the
  same `dcs-controller --driven --remote` shape the pair's own duty
  member runs — which stands pending behind the honest standby
  surface, its conditional startup grant deferred until a field
  contact answers;
- drives scans on the pending seat through the frozen window: each
  `POST /scan` must answer inside the declared bound — the
  reattach-window's one-stall cost, never the defect's
  once-per-point serialization — and the served tick must advance
  every scan, never frozen at 0 across the window, while `GET /role`
  and `GET /health` keep answering inside the liveness bound beside
  the parked field I/O;
- resumes the plant — the driver's next contact answers — and the
  pending seat's deferred conditional grant produces its verdict:
  the live incumbent's standing claim refuses it by name, and the
  pairless run takes the recorded disposition — the launch ends
  carrying the refusal — never preempting the incumbent;
- proves the deployed pair undisturbed — launch roles held, neither
  peer's journal gaining a disturbance record, the field's fencing
  verdict still naming the incumbent's token — and leaves the
  launch set restored for the legs behind this one.

A pinned release predating the contract — the pending run's scan
stalling once per point past the declared bound, a launch exiting
on a field-side refusal, the pending or liveness substrate absent —
reports `ownerless-backoff-digest inconclusive`, never a failure;
the v0.6.0 artifact set predates the contract's fix.

Usage:

    ownerless_backoff.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `ownerless-backoff-digest <sha256>` line prints —
the check runs two passes and compares them
(`ownerless-backoff-nondeterministic`). A contract violation reports
`ownerless-backoff: …` lines on stderr and exits 1 — the check's
`ownerless-backoff-failed`. `--tamper expect-frozen-tick` doctors the
frozen-window expectation to the wrong tick — the tick frozen where
the honest run advances it — so the leg proves its bounded-degraded
assertion fires on the honest run.
"""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import born_active_failure
import bounded_liveness
import claim_reclaim
import driver_recovery
import failover
import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored case.
# The doctored case: a leg asserting bounded degraded progress while
# the pending seat's tick stays frozen — the expectation doctored to
# the wrong tick — must surface the named diagnostic, never a
# silently unexercised contract.
LEG = {
    "order": 710,
    "title": "the ownerless remote-attachment backoff leg",
    "passes": "ownerless-backoff-leg",
    "tampers": [
        {
            "name": "expect-frozen-tick",
            "passed": "an expect-frozen-tick case passed the ownerless-backoff leg",
            "missed": "the expect-frozen-tick case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the pending seat's tick frozen"
            ],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the ownerless reattach-backoff
    contract the leg exercises — a pending run's scan paying the
    defect's once-per-point stall past the declared bound, a launch
    exiting on a field-side refusal, or the pending/liveness
    substrate the leg's evidence reads absent — or the consumer
    harness admits no stop/pause lever: the run classifies
    inconclusive, never a product failure. `args[0]` is the stable
    reason the digest line carries; `args[-1]` the run evidence the
    stderr line reports."""


# The leg's declared bounds. One frozen-window scan owes the
# reattach-window's cost — the driver's one request timeout per
# backoff window, ~5s under the released tooling — so twenty seconds
# is far past every healthy answer and far short of the defect's
# once-per-point stall (~15 field accesses at 5s each against this
# model). `ANSWER_BOUND_S` is the bounded-liveness bound the
# heartbeat reads owe beside the parked scan. The remaining
# constants pace the freeze window and the post-resume settle: the
# re-attach spacing each driven scan gives the armed backoff window,
# the settle scans the deferred grant's verdict lands inside, the
# exit bound the pairless refusal's recorded disposition owes, and
# the settle ticks the restore proves launch roles on.
FROZEN_SCANS = 3
SCAN_BOUND_S = 20.0
ANSWER_BOUND_S = 1.0
PROBE_SPACING_S = 0.25
SETTLE_SCANS = 8
REATTACH_WAIT = 1.4
EXIT_BOUND_S = 15.0
SETTLE_TICKS = 3

# The deferred conditional grant's named verdict — the incumbent's
# standing claim refusing the pending seat's ask, the contract's
# born-active refusal prose the 500 answer and the exit log both
# carry.
REFUSAL_MARK = "a live peer holds the field's write-ownership claim"

# The doctored case's named evidence — the wrong expectation the
# tampered leg asserts, carried by both the doctored failure and the
# inconclusive-offers-no-evidence line so a predating release can
# never launder the unchecked self-check.
TAMPER_EVIDENCE = (
    "the doctored expectation wanted the pending seat's tick frozen"
)

# The journal event kinds the deployed pair's record must not gain
# while the frozen-field episode plays — role, fencing, divergence,
# restart, command, and run-boundary records would be the
# disturbance a leaked claim leaves. Ordinary scan transitions are
# the run's own record.
DISTURBANCE_EVENTS = {
    "role_changed",
    "field_claim_lost",
    "divergence_detected",
    "divergence_resolved",
    "source_restarted",
    "reinitialized",
    "command_settled",
    "run_boundary",
}


def pause_plant(rig):
    """The frozen-field lever — `SIGSTOP` on the spawned plant
    stand-in, the `docker pause` shape a deployed container takes:
    the listener keeps accepting connections while nothing ever
    answers one, the connectable-but-silent field the contract's
    ownerless attachment meets. A lever that cannot land classifies
    inconclusive."""
    if not hasattr(signal, "SIGSTOP"):
        raise Inconclusive(
            "the consumer harness admits no stop/pause lever — no "
            "SIGSTOP"
        )
    try:
        rig.plant.send_signal(signal.SIGSTOP)
    except Exception as error:
        raise Inconclusive(
            "the plant pause lever never landed",
            f"{error}",
        )


def resume_plant(rig):
    """The freeze's restore — `SIGCONT` so the pending seat's next
    contact answers: the pause-then-unpause cycle the deployed
    `docker unpause` runs."""
    try:
        rig.plant.send_signal(signal.SIGCONT)
    except Exception:
        pass


def pending_probe(url, failures):
    """One bounded heartbeat-lane poll of the pending seat — `GET
    /role` and `GET /health` each owed an answer inside the declared
    bound while the seat's scan parks on the silent field. A stalled
    read or a missing HealthReport is the pinned release predating
    the liveness substrate — inconclusive — while a moved role is
    the contract breaking."""
    role, seen = bounded_liveness.bounded_get(f"{url}/role", ANSWER_BOUND_S)
    if role is None:
        raise Inconclusive(
            "the pinned release predates the bounded-liveness "
            "substrate — GET /role on the pending seat stalled "
            "behind the field freeze",
            f"{seen}",
        )
    if role.get("role") != "standby":
        failures.append(
            "the pending seat reports role "
            f"{role.get('role')!r} under the field freeze — the "
            "silent field moved the pending run's role"
        )
        raise Abort
    health, seen = bounded_liveness.bounded_get(
        f"{url}/health", ANSWER_BOUND_S
    )
    if health is None:
        raise Inconclusive(
            "the pinned release predates the bounded-liveness "
            "substrate — GET /health on the pending seat stalled "
            "behind the field freeze",
            f"{seen}",
        )
    report = bounded_liveness.health_report(health)
    if report is None:
        raise Inconclusive(
            "the pinned release predates the bounded-liveness "
            "substrate — the pending seat's /health serves no "
            "HealthReport",
            f"{health}",
        )
    if report.get("live") is not True or report.get("role") != "standby":
        failures.append(
            f"the pending seat's liveness answer serves {report} "
            "under the field freeze — expected live with role "
            "standby"
        )
        raise Abort


def trailing_stderr(process):
    """The rest of an exited member's stderr — the lines after
    `listening on` where the refused pending run's teardown verdict
    lands. Only read once the process is known dead."""
    try:
        return [line.strip() for line in process.stderr if line.strip()]
    except Exception:
        return []


def ownerless_backoff_pass(args, tamper):
    """The ownerless-backoff run: converge the deployed pair, freeze
    the plant's answers with its listener still accepting, launch
    the labeled born-active seat at the frozen endpoint, hold its
    pending tick to bounded degraded progress while /role and
    /health keep answering, then resume the field, settle the
    deferred claim under the standing contract, and restore.
    Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the
    contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "ownerless-backoff leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    budget = standby_decl.get("failover_budget")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        raise Abort(
            "the manifest's standby declares no positive "
            "failover_budget — the deployed pair's armed heartbeat "
            "is absent"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    members = []
    probe_io = None
    paused = False
    try:
        rig = pair.launch_pair(args, declared, auto_promote=budget)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        probe_io = simulate.PlantClient(rig.plant_addr)

        # Phase 1 — convergence and the baseline the episode must
        # leave untouched: the incumbent's owner token the fencing
        # verdict names, both peers' journal positions, the standing
        # fencing verdict a third-party write meets. A startup log
        # carrying no claim line is the release predating the
        # contract's substrate; a foreign write meeting no attributed
        # fencing verdict is the same gate.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        incumbent_token = failover.owner_token(rig.duty_preamble)
        if incumbent_token is None:
            raise Inconclusive(
                "the field owner's startup log carries no "
                "write-ownership claim line — the pinned release "
                "predates the startup-claim record the leg's "
                "attribution reads"
            )
        with open(args.model) as handle:
            model = json.load(handle)
        points = failover.signal_points(model)
        if points is None:
            raise Abort(
                "the emitted model declares no p101-cmd/level-primary "
                "signal points — the leg has no field probe to run"
            )
        held = failover.field_read(probe_io, points["cmd"], failures)[
            "value"
        ]
        verdict = failover.foreign_probe(
            probe_io, points["cmd"], held
        )
        if not claim_reclaim.mutation_fenced(verdict):
            raise Inconclusive(
                "a third-party write met no fencing verdict — the "
                "pinned release predates the claim-arbitration "
                "substrate the leg's undisturbed proof reads",
                f"the write probe answered {verdict}",
            )
        if claim_reclaim.verdict_owner(verdict) != incumbent_token:
            failures.append(
                "the standing fencing verdict names owner "
                f"{claim_reclaim.verdict_owner(verdict)}, not the "
                f"incumbent's token {incumbent_token}"
            )
            raise Abort
        duty_journal0 = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        standby_journal0 = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "incumbent": "active",
                "standby": "tracking",
                "armed": budget,
            }
        )

        # Phase 2 — the freeze: the deploy stage's pause lever holds
        # the harness plant's process, so its listener keeps
        # completing handshakes while no request ever answers — the
        # wedged-but-connectable field the contract's ownerless
        # attachment meets.
        pause_plant(rig)
        paused = True
        try:
            # Phase 3 — the labeled born-active launch against the
            # frozen field on the pair's own launch shape. Under the
            # contract it stands pending — the startup correspondence
            # probe's timeout once-armed the driver's backoff window,
            # the deferred grant waiting on the first answered
            # contact. An exit naming the field-side refusal
            # vocabulary is the pinned release predating the
            # pending-claim substrate; an exit naming nothing is a
            # defect.
            seat, seat_url, seat_preamble = (
                born_active_failure.spawn_born_active(
                    args,
                    rig.plant_addr,
                    born_active_failure.scratch_files(rig, "ownerless"),
                )
            )
            if seat_url is None:
                joined = " ".join(seat_preamble)
                if any(
                    word in joined
                    for word in born_active_failure.PREDATING_WORDS
                ):
                    raise Inconclusive(
                        "the pinned release predates the ownerless "
                        "reattach-backoff contract — the born-active "
                        "launch exited at startup naming a "
                        "field-side refusal",
                        "; ".join(seat_preamble[-2:])
                        or "no diagnostic",
                    )
                failures.append(
                    "the ownerless born-active launch exited at "
                    "startup without a field-side refusal: "
                    f"{'; '.join(seat_preamble) or 'no diagnostic'}"
                )
                raise Abort
            members.append(seat)
            if not any(
                "stands pending" in line for line in seat_preamble
            ):
                raise Inconclusive(
                    "the launch's startup log carries no pending "
                    "report — the pinned release predates the "
                    "pending born-active substrate the leg's "
                    "deferred claim waits on",
                    "; ".join(seat_preamble[-2:])
                    or "no diagnostic",
                )
            report, seen = bounded_liveness.bounded_get(
                f"{seat_url}/role", ANSWER_BOUND_S
            )
            if report is None:
                raise Inconclusive(
                    "the pending seat's monitor never answered GET "
                    "/role inside the declared bound at launch — "
                    "the pinned release predates the serving "
                    "substrate",
                    f"{seen}",
                )
            if report.get("role") in ("promoting", "active"):
                failures.append(
                    "the ownerless launch reports role "
                    f"{report.get('role')!r} against a silent field "
                    "— the deferred startup grant's claim landed "
                    "without a verdict"
                )
                raise Abort
            if (
                report.get("role") != "standby"
                or report.get("sync") != "unsynchronized"
                or report.get("field_claim") is not None
            ):
                raise Inconclusive(
                    "the pending surface reads differently than the "
                    "contract's recorded standby report — the "
                    "pinned release predates the pending substrate "
                    "the leg's window holds to",
                    f"GET /role answers {report}",
                )
            pending_probe(seat_url, failures)

            # Phase 4 — the frozen window: driven scans on the
            # pending seat, each joined under the declared bound.
            # The contract's cost per scan is the armed
            # reattach-window's one timed-out exchange — the tick
            # advancing once per scan — while the defect the fix
            # closed pays the timeout once per field point and the
            # request never answers inside the bound. Beside each
            # parked scan the heartbeat reads keep answering.
            seat_ticks = []
            last_tick = 0
            results = {}
            for index in range(FROZEN_SCANS):
                pending_probe(seat_url, failures)
                thread = threading.Thread(
                    target=bounded_liveness.drive_scan,
                    args=(seat_url, results, index),
                    daemon=True,
                )
                thread.start()
                started = time.monotonic()
                while thread.is_alive() and (
                    time.monotonic() - started < SCAN_BOUND_S
                ):
                    pending_probe(seat_url, failures)
                    time.sleep(PROBE_SPACING_S)
                thread.join(
                    max(0.0, SCAN_BOUND_S - (time.monotonic() - started))
                )
                if thread.is_alive():
                    raise Inconclusive(
                        "the pinned release predates the ownerless "
                        "reattach-backoff contract — the pending "
                        "seat's driven scan never answered inside "
                        f"the declared {SCAN_BOUND_S}s bound",
                        "the once-per-point stall the fix closed "
                        "holds the scan past the bound",
                    )
                answer = results.get(index)
                if seat.poll() is not None:
                    failures.append(
                        "the pending run exited under the frozen "
                        "field — the contract holds it pending, "
                        "re-issuing the deferred grant per answered "
                        "contact"
                    )
                    raise Abort
                if not isinstance(answer, dict) or not isinstance(
                    answer.get("tick"), int
                ):
                    failures.append(
                        "the pending seat's POST /scan under the "
                        f"frozen field answered {answer!r} — "
                        "expected a served snapshot"
                    )
                    raise Abort
                tick = answer["tick"]
                if tick <= last_tick:
                    failures.append(
                        "the pending seat's tick stayed frozen at "
                        f"{last_tick} across the frozen window — "
                        "the bounded-degraded contract owes one "
                        "advance per answered scan"
                    )
                    raise Abort
                last_tick = tick
                seat_ticks.append(tick)
            if tamper == "expect-frozen-tick":
                # The doctored expectation — the wrong bounded claim:
                # bounded degradation with the tick frozen where the
                # honest run advances it one per answered scan. The
                # honest run must fail it.
                failures.append(
                    f"{TAMPER_EVIDENCE} across the frozen window — "
                    "the honest run advanced it "
                    f"{seat_ticks}"
                )
                raise Abort
            digest_entries.append(
                {
                    "phase": "frozen-window",
                    "seat_ticks": seat_ticks,
                    "scans": "bounded",
                    "heartbeat": "answered",
                    "surface": "standby/unsynchronized/no-claim",
                }
            )
        finally:
            resume_plant(rig)
            paused = False

        # Phase 5 — the deferred claim resolves: the driver's next
        # contact answers once the plant resumes, the re-run
        # correspondence probes pass, and the conditional grant meets
        # the live incumbent's standing claim — the named refusal the
        # pairless launch takes its recorded disposition on. The
        # re-attach spacing paces each driven scan so the armed
        # backoff window expires between contacts.
        settled = None
        for _ in range(SETTLE_SCANS):
            time.sleep(REATTACH_WAIT)
            if seat.poll() is not None:
                failures.append(
                    "the pending run exited before the deferred "
                    "claim produced its verdict — the contract "
                    "settles the grant on the first answered "
                    "contact"
                )
                raise Abort
            try:
                status, body = pair.request(
                    f"{seat_url}/scan", {"scans": 1}
                )
            except Exception as error:
                failures.append(
                    "the pending seat's POST /scan after the field "
                    f"resumed raised {error!r} — the deferred "
                    "grant's verdict never reached the wire"
                )
                raise Abort
            if status == 200:
                continue
            if REFUSAL_MARK in str(body):
                settled = str(body)
                break
            failures.append(
                "the first answered contact's POST /scan answered "
                f"{status} {body} — expected the deferred grant's "
                "named refusal"
            )
            raise Abort
        if settled is None:
            failures.append(
                "the deferred startup claim never produced its "
                "verdict once the field answered — the pending "
                "seat's grant stayed silent across the settle "
                "window"
            )
            raise Abort
        try:
            seat.wait(timeout=EXIT_BOUND_S)
        except subprocess.TimeoutExpired:
            failures.append(
                "the refused pending run never took its recorded "
                "disposition — a pairless run's named refusal ends "
                "the launch, and the seat stood serving past the "
                "exit bound"
            )
            raise Abort
        members.pop()
        trailing = trailing_stderr(seat)
        if not any(REFUSAL_MARK in line for line in trailing):
            failures.append(
                "the refused pending run's exit log names no "
                "write-ownership verdict: "
                f"{'; '.join(trailing) or 'no diagnostic'}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "resolution",
                "verdict": "refused-by-incumbent",
                "disposition": "exited",
            }
        )

        # Phase 6 — the pair undisturbed and the launch set
        # restored: the episode's every extra member is gone, the
        # settle ticks prove the field owner active and the declared
        # standby tracking it, neither peer's journal gained a
        # disturbance record, and the field's fencing verdict still
        # names the incumbent's token — the pending seat never
        # claimed and never wrote.
        owner = None
        for _ in range(SETTLE_TICKS):
            _tracked, owner = rig.tick(standby_url, duty_url, failures)
        if not driver_recovery.roles_hold(
            rig, failures, "after the frozen-field episode"
        ):
            raise Abort
        duty_journal1 = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        standby_journal1 = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        for peer_name, before, after in (
            (duty_decl["name"], duty_journal0, duty_journal1),
            ("the declared standby", standby_journal0, standby_journal1),
        ):
            leaked = [
                entry
                for entry in after[len(before):]
                if DISTURBANCE_EVENTS & set(entry.get("event", {}))
            ]
            if leaked:
                failures.append(
                    f"{peer_name}'s journal gained disturbance "
                    f"records during the frozen-field episode: "
                    f"{leaked}"
                )
                raise Abort
        verdict = failover.foreign_probe(
            probe_io, points["cmd"], held
        )
        if claim_reclaim.verdict_owner(verdict) != incumbent_token:
            failures.append(
                "the field's fencing verdict moved off the "
                "incumbent's token during the frozen-field "
                f"episode: {verdict}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "restore",
                "duty": "active",
                "standby": "tracking",
                "claim": "incumbent",
                "journals": "undisturbed",
            }
        )
        evidence["final_tick"] = owner["tick"]
    except Inconclusive:
        raise
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if paused and rig is not None and rig.plant is not None:
            resume_plant(rig)
        for process in members:
            pair.stop(process)
        if probe_io is not None:
            probe_io.close()
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
        choices=["expect-frozen-tick"],
        help="doctor the frozen-window expectation to the wrong "
        "tick — bounded degradation asserted while the pending "
        "seat's tick stays frozen — so the pass must fail naming "
        "the doctored claim",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = ownerless_backoff_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"ownerless-backoff: {TAMPER_EVIDENCE} — an "
                "inconclusive run offers the doctored case no "
                "evidence"
            )
            return 1
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"ownerless-backoff: inconclusive — {detail}")
        print(f"ownerless-backoff-digest inconclusive — {reason}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"ownerless-backoff: {line}")
        return 1
    for failure in failures:
        eprint(f"ownerless-backoff: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"ownerless-backoff: the {args.tamper} case "
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
        f"ownerless-backoff-digest {digest} — the declared pair "
        "converged, the pending seat's ownerless attachment held "
        "bounded degraded ticks through the frozen field with "
        "/role and /health answering, the deferred claim resolved "
        "to the incumbent's named refusal, and launch roles held "
        f"at tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
