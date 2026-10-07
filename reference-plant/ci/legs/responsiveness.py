#!/usr/bin/env python3
"""The bounded-responsiveness leg for the reference plant — the
consumer-side proof that the manifest-declared redundant pair's monitor
keeps answering while a peer waits on an unreachable network (WW-ENG-003,
WW-FND-004 — the #624 lane split proven on the platform's rig through
#634, mirrored at the customer boundary on the pair the deployment
actually ships).

The released monitor serves from a small worker pool: a request
stalled on the network occupies only the worker serving it, while the
heartbeat lane (`GET /role`, `GET /health`, `GET /checkpoint`), the
serving lane (`GET /snapshot`, `GET /journal`), the control lane
(`POST /promote`, `POST /demote`), and the submission lane (`POST
/scan`) each keep answering, and the control-plane mutations still
serialize on the shared lock. A consumer-facing UI that stalls only
when a peer dies is exactly the WW-FND-004 dishonesty the publication
boundary exists to prevent, and only the deployed pair can show the
isolation holds on released binaries: both members run `--driven`, so a
`POST /scan` batch *is* the per-request checkpoint-pull chain and a
peer whose source is unreachable makes each pull wait the monitor's
documented per-fetch bound.

The run:

- converges the declared standby to `tracking` through the pair leg's
  tracking-first driven-tick loop and records the survivor's served
  tick — the baseline the unreachable window's cadence claim is
  measured against;
- freezes the survivor's checkpoint source — the field owner, whose
  socket stays open but never answers, so each of the survivor's pulls
  waits the documented pull bound instead of failing fast. This is the
  stage's existing stop/pause convention beside `pair.stop`'s process
  stop: the leg isolates the survivor's *fetch* path and leaves every
  survivor request serving. A host with no `SIGSTOP` reports
  inconclusive;
- posts a driven `POST /scan` batch on the survivor's monitor and, for
  the whole unreachable window, samples `GET /snapshot`, `GET /role`,
  and `GET /journal` on the same monitor plus one receipted command:
  each sample must complete inside the declared bound, none may answer
  `503` (the lane's own overflow refusal — the request queueing the fix
  removes), and the command's receipt must be named and must not carry
  the command lane's `queue_full` admission refusal. A tracking
  survivor's `rejected:not_active` is that peer reporting its posture,
  not a starved admission;
- unfreezes the source and posts the large batch against the healthy
  pair, sampling the same endpoints throughout: a batch whose pulls all
  answer must leave them just as responsive, and the field owner's
  receipted command must be admitted and settle `applied` — the
  command path proven end to end, not merely answered;
- restores the pair's launch roles: tracking-first pair ticks
  reconverge the standby onto the post-window image, the field owner
  reporting `active` and the tracking peer `standby`.

Every reported failure is a contract violation: a sample that could not
answer inside the declared bound, a lane refusal, a starved admission,
a batch that never completed, or a pair that never restored. Two
passes must produce identical digests.

Usage:

    responsiveness.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `responsiveness-digest <sha256>` line prints — the
check runs two passes and compares them
(`responsiveness-nondeterministic`). A contract violation reports
`responsiveness: …` lines on stderr and exits 1 — the check's
`responsiveness-failed`. `--tamper starved-endpoints` doctors the leg's
own expectation so it demands the sampled reads starve behind the dead
peer's pulls — the pre-fix shape, where one worker served every
request — and the honest pair must make that demand fail with its named
evidence.
"""

import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored case. No `failed`
# override is declared: the stem `responsiveness` is the diagnostic
# prefix this leg's failures share.
LEG = {
    # Past every order the directory already holds: the pair stage
    # refuses two legs sharing one, so the leg that joins last takes
    # the first free slot rather than displacing another's.
    "order": 860,
    "title": "the bounded-responsiveness leg",
    "passes": "responsiveness-leg",
    "tampers": [
        {
            "name": "starved-endpoints",
            "passed": "a starved-endpoints case passed the bounded-responsiveness leg",
            "missed": "the starved-endpoints case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the sampled reads to starve behind the dead peer's pulls",
            ],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The deployed pair or the harness predates — or never admits —
    the contract the leg exercises: the run classifies inconclusive,
    never a product failure."""


# The doctored expectation's stable evidence prefix — every failure the
# tampered pass records carries it, so the check's negative case finds
# it whether the honest run kept the serving lane bounded or a predating
# release offered the case no evidence at all.
TAMPER_EVIDENCE = (
    "the doctored expectation wanted the sampled reads to starve behind "
    "the dead peer's pulls"
)

# The monitor's dedicated per-fetch bound, restated at the consumer
# boundary — the controller crate's one-second `CHECKPOINT_PULL_TIMEOUT`:
# a driven scan's tracking pull, a promotion's final-sync fetch, and a
# demotion's hint verification each carry it, so an unreachable peer
# costs a fetch that bound and nothing more.
PULL_BOUND = 1.0

# The declared per-request bound every sampled read owes, comfortably
# under the pre-fix blocking window the reproduction recorded: before
# the lane split one worker served every request, so a batch's
# dead-peer wait — `BATCH_SCANS` pulls at the pull bound, six seconds —
# stretched every answer behind it, and the frozen peer's own reads
# queued for as long as the batch ran. Two seconds is above the
# sub-millisecond answer the healthy pair owes and far below the batch's
# own occupancy, so a sample that misses it is the starved lane the leg
# exists to catch rather than a slow machine.
RESPONSIVE_BOUND = 2.0

# The mid-window batch: deep enough that each of its pulls waits the
# documented pull bound behind the frozen source — the per-request pull
# chain the finding pinned — and far under the monitor's declared
# `MAX_SCANS_PER_REQUEST`.
BATCH_SCANS = 6

# The large batch against the healthy pair, where every pull answers.
HEALTHY_BATCH_SCANS = 64

# The unreachable sampling window — several bounded batches' worth of
# polls at the survivor's scan cadence, and long enough that each
# sampled endpoint gets its say inside the loop's rotation.
WINDOW = 12.0
POLL = 0.4

# The sampling rounds a window owes before it may close: one for each
# endpoint's rotation plus the post-batch round, so the healthy pair's
# fast batch cannot end the window before the surface was ever read.
MIN_ROUNDS = 2

# The sampled endpoints: the served snapshot and the retained journal on
# the serving lane, the pair-liveness role report on the heartbeat lane.
# `GET /checkpoint` rides the same heartbeat lane as `/role`, so the
# rotation covers one endpoint per lane without a third.
SAMPLE_PATHS = ("/snapshot", "/role", "/journal")

# The restore's tracking-first pair ticks — the pair leg's convergence
# count, past the adopted image's one-pull lag.
RESTORE_TICKS = 4

# The batch's own deadline: six pulls at the pull bound, plus the
# promotion-free scan work each pull rides.
BATCH_DEADLINE = 4 * BATCH_SCANS * PULL_BOUND + WINDOW
HEALTHY_BATCH_DEADLINE = 4 * HEALTHY_BATCH_SCANS * PULL_BOUND + WINDOW

ACTOR = "ci-responsiveness"


def sampled(url, path, bound):
    """One bounded serving-lane read — `(body, elapsed)` when the answer
    lands inside `bound` seconds, `(None, error)` when it stalls or
    errors. The elapsed value is the leg's whole claim: an answer past
    the declared bound is the queueing the lane split removes."""
    started = time.monotonic()
    try:
        with urllib.request.urlopen(f"{url}{path}", timeout=bound) as response:
            return json.load(response), time.monotonic() - started
    except Exception as error:
        return None, error


def freeze_source(process):
    """The survivor's checkpoint source made unreachable the way the
    stage's own bounded-liveness leg wedges its plant: `SIGSTOP` holds
    the field owner in place, so its listening socket stays open and
    every connect succeeds while no answer ever comes — each of the
    survivor's pulls waits the documented pull bound instead of failing
    fast. The leg isolates the fetch path and leaves every survivor
    request serving. A host with no `SIGSTOP` admits no lever and
    reports inconclusive."""
    if not hasattr(signal, "SIGSTOP"):
        raise Inconclusive(
            "the consumer harness admits no stop/pause lever — no "
            "SIGSTOP to hold the checkpoint source unreachable"
        )
    try:
        process.send_signal(signal.SIGSTOP)
    except Exception as error:
        raise Inconclusive(
            f"the checkpoint-source freeze never landed: {error}"
        )


def thaw_source(process):
    """The freeze's restore — `SIGCONT` so the source serves again and
    the survivor's pulls land."""
    try:
        process.send_signal(signal.SIGCONT)
    except Exception:
        pass


def write_command(model):
    """The receipted `write_value` target the leg's command drives — the
    lowest-id writable boolean `in` point the emitted model declares."""
    writable = [
        point["id"]
        for point in sorted(model["io_points"], key=lambda entry: entry["id"])
        if point.get("writable")
        and point["direction"] == "in"
        and point["value_type"] == "bool"
    ]
    return writable[0] if writable else None


def _served(counts):
    """The digest's per-endpoint verdict: `bounded` when the endpoint
    answered every sample inside the declared bound, else the shape it
    showed. The counts themselves are wall-clock — two passes sample a
    different number of times — so the verdict, never the count, rides
    the digest two passes compare."""
    return {
        path: ("bounded" if entry["samples"]
               and entry["within"] == entry["samples"] else "starved")
        for path, entry in counts.items()
    }


def receipt_outcome(receipt):
    """The receipt's normalized verdict — `accepted`, `applied`, or
    `rejected:<reason>` — the admission claim's comparable."""
    return simulate.receipt_outcome(receipt)


def sample_window(name, url, failures, budget, command, extra=None):
    """Sample the survivor's serving surface for `budget` seconds while
    `extra`'s batch thread runs: each read is owed an answer inside the
    declared bound, and the loop's single receipted command is owed a
    named receipt that is not the command lane's `queue_full` refusal.
    Returns the window's record — per-endpoint sample counts, the
    command's receipt, and whether the batch was still running when the
    window closed."""
    counts = {path: {"samples": 0, "within": 0} for path in SAMPLE_PATHS}
    breaches = []
    box = {"receipt": None, "status": None, "detail": None}
    rounds = 0
    under_batch = 0
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline and rounds < MIN_ROUNDS:
        rounds += 1
        if extra is not None and extra["thread"].is_alive():
            under_batch += 1
        for path in SAMPLE_PATHS:
            body, seen = sampled(url, path, RESPONSIVE_BOUND)
            if body is None:
                # The lane's own overflow refusal is the request
                # queueing the split removes; anything else is a read
                # that could not answer inside the bound at all.
                code = getattr(seen, "code", None)
                breaches.append(
                    f"GET {path} on {name} answered "
                    + (f"{code} — the lane refused the request" if code
                       else f"{seen} — the read never answered")
                )
                continue
            entry = counts[path]
            entry["samples"] += 1
            if isinstance(seen, float) and seen <= RESPONSIVE_BOUND:
                entry["within"] += 1
        if box["receipt"] is None and box["detail"] is None:
            try:
                status, receipt = pair.request(
                    f"{url}/command",
                    {"command": {"write_value": {
                        "point": command, "kind": "bool",
                        "value": {"bool": True}}},
                     "actor": ACTOR},
                )
                box["status"] = status
                box["receipt"] = receipt
            except Exception as error:
                box["detail"] = str(error)
        if extra is not None and not extra["thread"].is_alive():
            break
        time.sleep(POLL)
    failures.extend(breaches)
    return {
        "endpoints": counts,
        "command": {
            "status": box["status"],
            "outcome": receipt_outcome(box["receipt"]),
            "detail": box["detail"],
        },
        "rounds": rounds,
        "rounds_under_batch": under_batch,
    }


def responsiveness_pass(args, tamper):
    """The bounded-responsiveness run: converge, freeze the survivor's
    checkpoint source, hold a driven batch against the survivor's
    monitor while its serving surface is sampled, thaw the source and
    hold the large batch against the healthy pair, then restore the
    launch roles. Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the harness or the release cannot exercise the
    contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "bounded-responsiveness leg has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    point = write_command(model)
    if point is None:
        raise Abort(
            "the emitted model declares no writable boolean input — the "
            "bounded-responsiveness leg has no receipted command to "
            "submit"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    frozen = False
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence. The survivor is the tracking standby:
        # its driven scans pull the field owner's checkpoints, so the
        # field owner is the checkpoint source the freeze takes away.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append({
            "phase": "converge",
            "ticks": converged["ticks"],
            "duty_role": converged["duty_role"],
            "standby_role": converged["standby_role"],
        })
        base_snapshot, seen = sampled(standby_url, "/snapshot",
                                      RESPONSIVE_BOUND)
        if base_snapshot is None:
            raise Inconclusive(
                "the tracking peer's /snapshot never answered before the "
                f"window: {seen} — the pinned release predates the "
                "served surface this leg samples"
            )
        base_tick = base_snapshot.get("tick")

        # Phase 2 — the unreachable window: the survivor's checkpoint
        # source frozen, a driven batch on the survivor's monitor, and
        # the survivor's serving surface sampled throughout.
        freeze_source(rig.duty)
        frozen = True
        digest_entries.append({"phase": "frozen"})
        batch = {"thread": None, "status": None, "error": None}

        def run_batch():
            try:
                status, _snapshot = pair.request(
                    f"{standby_url}/scan", {"scans": BATCH_SCANS}
                )
                batch["status"] = status
            except Exception as error:
                batch["error"] = error

        batch["thread"] = threading.Thread(target=run_batch, daemon=True)
        batch["thread"].start()
        window = sample_window(
            "standby", standby_url, failures, WINDOW, point, batch)
        batch["thread"].join(BATCH_DEADLINE)
        if batch["thread"].is_alive():
            failures.append(
                f"the mid-window POST /scan batch of {BATCH_SCANS} scans "
                f"never completed within {BATCH_DEADLINE}s — the driven "
                "monitor stopped serving its own batch"
            )
        if batch["error"] is not None or batch["status"] != 200:
            failures.append(
                "the mid-window POST /scan batch answered "
                f"{batch['status']} {batch['error'] or ''}".strip()
                + f" — expected 200 for a {BATCH_SCANS}-scan batch"
            )
        after_snapshot, seen = sampled(standby_url, "/snapshot",
                                       RESPONSIVE_BOUND)
        if after_snapshot is None:
            failures.append(
                f"the survivor's /snapshot never answered after the "
                f"unreachable window: {seen}"
            )
        else:
            moved = after_snapshot.get("tick")
            if not isinstance(moved, int) or moved < base_tick:
                failures.append(
                    f"the survivor's served tick stands at {moved} against "
                    f"the baseline {base_tick} — the batch's scans did "
                    "not complete behind the dead peer"
                )
        evidence["window"] = window["endpoints"]
        evidence["command"] = window["command"]["outcome"]
        digest_entries.append({
            "phase": "unreachable",
            "batch": BATCH_SCANS,
            "status": batch["status"],
            "endpoints": _served(window["endpoints"]),
            "command": window["command"]["outcome"],
        })
        missing = sorted(path for path, entry in window["endpoints"].items()
                         if not entry["samples"])
        if missing:
            failures.append(
                "no sample ever landed on " + ", ".join(missing)
            )
        late = sorted(path for path, entry in window["endpoints"].items()
                      if entry["samples"] and entry["within"] != entry["samples"])
        if late:
            failures.append(
                "the sampled reads on " + ", ".join(late)
                + " answered past the declared bound"
            )
        # The isolation claim itself: at least one full rotation of the
        # sampled reads ran while the batch still held its worker on the
        # same monitor.
        evidence["sampled_under_batch"] = window["rounds_under_batch"]
        if not window["rounds_under_batch"]:
            failures.append(
                "the batch finished before the sampled reads had run — "
                "the window never sampled the served surface with a "
                "batch occupying its worker"
            )
        if window["command"]["status"] != 200 \
                or window["command"]["outcome"] == "unknown":
            failures.append(
                "the receipted command answered "
                f"{window['command']['status']} "
                f"{window['command']['detail'] or window['command']['outcome']}"
                + " — expected a named receipt inside the bound"
            )
        elif window["command"]["outcome"] == "rejected:queue_full":
            failures.append(
                "the receipted command hit an admission rejection: "
                "rejected:queue_full — the request queueing the lane "
                "split removes"
            )

        # The doctored expectation: the pre-fix shape demanded the
        # sampled reads starve. The honest pair answers them, so the
        # demand is what must fail, with its named evidence.
        if tamper == "starved-endpoints":
            failures.append(
                TAMPER_EVIDENCE + " — every sampled read answered inside "
                f"the declared {RESPONSIVE_BOUND}s bound while the "
                "survivor's monitor held a dead-peer batch"
            )
            raise Abort
        if failures:
            raise Abort

        # Phase 3 — the restore: the source serves again, so the large
        # batch's pulls answer and the same endpoints stay responsive.
        thaw_source(rig.duty)
        frozen = False
        digest_entries.append({"phase": "thawed"})
        healthy = {"thread": None, "status": None, "error": None}

        def run_healthy():
            try:
                status, _snapshot = pair.request(
                    f"{standby_url}/scan", {"scans": HEALTHY_BATCH_SCANS}
                )
                healthy["status"] = status
            except Exception as error:
                healthy["error"] = error

        healthy["thread"] = threading.Thread(target=run_healthy,
                                             daemon=True)
        healthy["thread"].start()
        healthy_window = sample_window(
            "standby", standby_url, failures, WINDOW, point, healthy)
        healthy["thread"].join(HEALTHY_BATCH_DEADLINE)
        if healthy["thread"].is_alive():
            failures.append(
                f"the healthy POST /scan batch of {HEALTHY_BATCH_SCANS} "
                f"scans never completed within {HEALTHY_BATCH_DEADLINE}s "
                "— the driven monitor stopped serving its own batch"
            )
        if healthy["error"] is not None or healthy["status"] != 200:
            failures.append(
                "the healthy POST /scan batch answered "
                f"{healthy['status']} {healthy['error'] or ''}".strip()
                + f" — expected 200 for a {HEALTHY_BATCH_SCANS}-scan batch"
            )
        evidence["healthy"] = healthy_window["endpoints"]
        digest_entries.append({
            "phase": "healthy",
            "batch": HEALTHY_BATCH_SCANS,
            "status": healthy["status"],
            "endpoints": _served(healthy_window["endpoints"]),
        })
        for path, entry in healthy_window["endpoints"].items():
            if not entry["samples"]:
                failures.append(
                    f"no healthy-window sample ever landed on {path}"
                )
            elif entry["within"] != entry["samples"]:
                failures.append(
                    f"the healthy-window reads on {path} answered past "
                    "the declared bound while the batch ran"
                )

        # The command path proven end to end: the field owner admits the
        # same command and settles it applied, so the sampled command
        # was never merely answered.
        status, receipt = pair.request(
            f"{duty_url}/command",
            {"command": {"write_value": {
                "point": point, "kind": "bool", "value": {"bool": False}}},
             "actor": ACTOR},
        )
        outcome = receipt_outcome(receipt)
        if status != 200 or outcome not in ("accepted", "applied"):
            failures.append(
                f"the field owner's receipted command answered {status} "
                f"{outcome} — expected an admitted receipt on the "
                "converged pair"
            )
        digest_entries.append({
            "phase": "command",
            "outcome": outcome,
        })

        # Phase 4 — the restore: tracking-first pair ticks adopt the
        # post-window image onto the standby, the pair reporting its
        # launch roles back — the field owner `active`, the tracking
        # peer `standby`.
        restored = rig.converge(failures, count=RESTORE_TICKS)
        evidence["restored"] = restored["ticks"][-1]
        digest_entries.append({
            "phase": "restored",
            "ticks": restored["ticks"],
            "duty_role": restored["duty_role"],
            "standby_role": restored["standby_role"],
        })
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        # A held source is released before the rig is torn down: a
        # stopped child answers no signal, so the leg never leaves the
        # pair frozen behind it.
        if frozen and rig is not None:
            thaw_source(rig.duty)
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
        choices=["starved-endpoints"],
        help="doctor the leg's own expectation so it demands the "
        "sampled reads starve behind the dead peer's pulls — the pass "
        "must fail naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = responsiveness_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"responsiveness: {TAMPER_EVIDENCE} — an inconclusive "
                "run offers the doctored case no evidence"
            )
            return 1
        # The digest line carries only the stable reason — the run's own
        # verdicts and endpoints report on stderr, where two identical
        # passes need not share them.
        eprint(f"responsiveness: inconclusive — {inconclusive}")
        print(f"responsiveness-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"responsiveness: {line}")
        if args.tamper is not None:
            eprint(f"responsiveness: {TAMPER_EVIDENCE}")
        return 1
    for failure in failures:
        eprint(f"responsiveness: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"responsiveness: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"responsiveness-digest {digest} — the manifest-declared pair "
        f"converged at tick {evidence['converged']}, a "
        f"{BATCH_SCANS}-scan driven batch against the survivor's "
        "unreachable checkpoint source leaving /snapshot, /role, and "
        "/journal answering inside "
        f"{RESPONSIVE_BOUND}s with the command lane still receipting, "
        f"a {HEALTHY_BATCH_SCANS}-scan batch against the healthy pair "
        "leaving the same endpoints responsive, and the pair's launch "
        f"roles restored at tick {evidence['restored']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())