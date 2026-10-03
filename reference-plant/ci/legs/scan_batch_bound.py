#!/usr/bin/env python3
"""The scan-batch bound leg for the reference plant — the consumer-side
proof that the deployed pair's driven `POST /scan` accepts only
bounded batches: a `scans` past the monitor's declared per-request
bound is refused `400` by name before the first scan, and a batch
whose client dies mid-flight still terminates inside its own request
(WW-ENG-003, WW-FND-004 — the #1203 unbounded-scan contract the
monitor's own tests pin in-workspace, mirrored at the customer
boundary on the manifest-declared pair, whose controllers run
`--driven` exactly as the deployment the finding reproduced did).

The pair leg (`ci/legs/pair.py`) proves the declared pair runs and
switches; the bounded-liveness leg proves its reads stay answered
under a wedged client. This leg exercises the driven lane's work
bound on the same deployment: without it one `POST /scan` carrying a
huge `scans` count hands a single client the run's whole timeline and
pins its submission worker for as long as it cares to — and a client
gone mid-batch leaves nothing to cancel the runaway work. The
monitor's socket API hands a request no liveness to poll, so the
declared bound is the cancellation: the batch that always terminates
is the batch that is bounded. The run:

- converges the declared standby to `tracking` through the pair
  leg's driven-tick loop, recording the field owner's served tick and
  the shared plant's step counter — the `ping` probe's reported tick,
  the counter every field-owning scan's plant step advances;
- posts an over-bound `scans` to the manifest-declared field owner —
  one past the declared bound, kept small so a release predating the
  bound still terminates its accepted batch: the named `400` refusal
  must carry the asked count and the declared bound, and neither the
  served tick nor the shared plant's step counter may move — a
  refused request runs nothing;
- starts a batch at the declared bound on a raw connection, watches
  the served tick start moving so the sever lands provably
  mid-flight, then drops the client: the batch must still terminate —
  the served tick and the shared plant's step counter each climb to
  the batch's bound and hold, never advancing further on the dead
  client's behalf, and the next bounded request answers — the
  abandoned worker freed with the batch's end;
- restores the pair's launch roles: tracking-first pair ticks
  reconverge the standby onto the post-batch image, the field owner
  reports `active` and the tracking peer `standby`.

The contract postdates the pinned release line: where the launched
tooling predates the per-request bound, the probe's own answer — the
over-bound batch accepted and run — is the pre-contract shape and the
leg reports `scan-batch-bound-digest inconclusive` rather than
asserting until the manifest repins a release carrying the contract;
every released artifact set predates it until the fix lands and a
release carries it.

Usage:

    scan_batch_bound.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `scan-batch-bound-digest <sha256>` line prints — the
check runs two passes and compares them
(`scan-batch-bound-nondeterministic`). A contract violation reports
`scan-batch-bound: …` lines on stderr and exits 1 — the check's
`scan-batch-bound-failed`. `--tamper under-bound` doctors the leg's
own bound record so its probe lands inside the monitor's real bound —
the batch it calls over-bound runs unrefused, and the leg must report
the named refusal that never came rather than pass an unexercised
contract.
"""

import argparse
import json
import os
import re
import socket
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored case.
# The doctored case: a leg whose declared bound is cut so its probe
# lands inside the monitor's real bound must surface the named
# diagnostic on the honest refusal assertion — the batch it called
# over-bound running unrefused — never a silently unexercised bound.
LEG = {
    "order": 580,
    "title": "the scan-batch bound leg",
    "passes": "scan-batch-bound-leg",
    "tampers": [
        {
            "name": "under-bound",
            "passed": "an under-bound case passed the scan-batch-bound leg",
            "missed": "the under-bound case did not report its named diagnostic",
            "evidence": ["the doctored under-bound probe ran unrefused"],
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


# The declared per-request bound — the monitor's
# `MAX_SCANS_PER_REQUEST`, restated at the consumer boundary: the
# contract is that a `scans` past it is refused by name and a batch at
# it still terminates inside its request. The probe asks one past the
# bound and no more — a predating release accepts and runs whatever it
# is sent, so the over-bound count stays a size it can finish.
SCAN_BATCH_BOUND = 256
DOCTORED_BOUND = 128

# The raw-connection timings the severed-batch phase runs: the window
# for the batch to show its first scan (provably in flight before the
# sever), the deadline for the bounded batch to run out, the quiet
# window proving the dead client's batch holds its end rather than
# climbing on, and the poll cadence — all wall-clock slack around work
# that lands in milliseconds on the simulated plant.
IN_FLIGHT_TIMEOUT_S = 30.0
BATCH_SETTLE_TIMEOUT_S = 30.0
HOLD_SETTLE_S = 0.2
POLL_INTERVAL_S = 0.02

# The tracking-first pair ticks the restore phase drives — the pair
# leg's convergence count, past the adopted image's one-pull lag.
RESTORE_TICKS = 4


def plant_tick(rig):
    """The shared plant's step counter — the `ping` probe's tick, no
    field access and no mutation. A `ping` unanswered or answered
    without a tick is the leg's evidence surface missing: the pinned
    release predates it, inconclusive rather than a violation."""
    try:
        answer = rig.plant_io.request({"op": "ping"})
    except Exception as error:
        raise Abort(f"the shared plant's ping probe raised {error!r}")
    if (
        not isinstance(answer, dict)
        or answer.get("result") != "alive"
        or not isinstance(answer.get("tick"), int)
    ):
        raise Inconclusive(
            f"the shared plant answered ping with {answer} — the "
            "pinned release predates the step counter this leg reads"
        )
    return answer["tick"]


def served_tick(url, failures):
    """The monitor's served run tick — `GET /snapshot`'s tick field."""
    snapshot = pair.get(f"{url}/snapshot", "GET /snapshot", failures)
    return snapshot.get("tick")


def severed_batch(url, scans):
    """Open a raw connection to the monitor and post a `scans`-sized
    batch on it — the client the leg severs mid-flight. Returns the
    socket with the request fully sent; its answer is never read."""
    address = url.removeprefix("http://")
    host, _, port = address.rpartition(":")
    client = socket.create_connection((host, int(port)), timeout=30)
    body = json.dumps({"scans": scans}).encode()
    client.sendall(
        b"POST /scan HTTP/1.1\r\n"
        + f"Host: {address}\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + body
    )
    return client


def scan_batch_pass(args, tamper):
    """The scan-batch bound run: converge, refuse the over-bound probe
    by name, sever an at-bound batch's client mid-flight and prove the
    batch terminates, then restore the launch roles. Returns
    `(digest_entries, evidence, failures)`; raises `Inconclusive`
    where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "scan-batch-bound leg has nothing to exercise"
        )
    # The bound the leg asserts — its own record, the doctored case
    # cutting it so the probe lands inside the monitor's real bound.
    bound = DOCTORED_BOUND if tamper == "under-bound" else SCAN_BATCH_BOUND
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence, then the baselines the refusal and
        # the severed batch are measured against: the field owner's
        # served tick and the shared plant's step counter.
        converged = rig.converge(failures)
        base_tick = converged["owner"]["tick"]
        base_plant = plant_tick(rig)
        evidence["converged"] = base_tick
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # Phase 2 — the over-bound probe: one scan past the leg's
        # declared bound. The named refusal must carry the asked count
        # and the bound — and move nothing: the refusal lands before
        # the first scan. An accepted probe is not a violation to
        # assert — it is the pre-contract shape itself, the release
        # running whatever a client asks.
        probe = bound + 1
        status, answer = pair.request(
            f"{duty_url}/scan", {"scans": probe}
        )
        text = (
            answer if isinstance(answer, str) else json.dumps(answer)
        )
        if status == 200:
            moved = answer.get("tick") if isinstance(answer, dict) else "?"
            if tamper == "under-bound":
                failures.append(
                    "the doctored under-bound probe ran unrefused — "
                    f"POST /scan answered 200 on a {probe}-scan batch "
                    f"(the leg's doctored bound {bound}), the tick "
                    f"moving {base_tick}→{moved}; the named refusal "
                    "the leg asserted never came"
                )
                raise Abort
            raise Inconclusive(
                f"the over-bound POST /scan ran — scans {probe} "
                f"answered 200 with the served tick moved "
                f"{base_tick}→{moved}; the pinned release predates "
                "the scan-batch bound contract"
            )
        named = re.search(
            r"refused: scans (\d+) exceeds the per-request bound of "
            r"(\d+)",
            text,
        )
        if status != 400 or named is None:
            failures.append(
                f"the over-bound POST /scan answered {status} "
                f"{text!r} — expected the named refusal carrying the "
                "asked count and the declared bound"
            )
            raise Abort
        asked, named_bound = int(named.group(1)), int(named.group(2))
        if asked != probe or named_bound != bound:
            failures.append(
                f"the refusal reads {named.group(0)!r} — it must name "
                f"the asked count {probe} and the declared bound "
                f"{bound}"
            )
            raise Abort
        held_tick = served_tick(duty_url, failures)
        if held_tick != base_tick:
            failures.append(
                f"the refused batch moved the run's tick "
                f"{base_tick}→{held_tick} — a request past the bound "
                "is refused before the first scan"
            )
        held_plant = plant_tick(rig)
        if held_plant != base_plant:
            failures.append(
                f"the refused batch stepped the shared plant "
                f"{base_plant}→{held_plant} — a request past the "
                "bound runs nothing"
            )
        if failures:
            raise Abort
        evidence["refused"] = probe
        digest_entries.append(
            {
                "phase": "refused",
                "scans": probe,
                "refusal": text,
            }
        )

        # Phase 3 — the severed batch: a `scans` at the declared bound
        # posted on a raw connection, its tick watched until the batch
        # is provably in flight, then the client dropped. The dead
        # client's behalf runs out inside the bounded remainder: the
        # served tick and the shared plant's step counter climb to the
        # batch's bound and hold, and the next bounded request answers
        # — the abandoned worker freed with the batch's end.
        target = base_tick + bound
        plant_target = base_plant + bound
        client = severed_batch(duty_url, bound)
        try:
            began = time.monotonic()
            tick = served_tick(duty_url, failures)
            while tick <= base_tick:
                if time.monotonic() - began > IN_FLIGHT_TIMEOUT_S:
                    failures.append(
                        "the at-bound batch never started — the served "
                        f"tick held {base_tick} for "
                        f"{IN_FLIGHT_TIMEOUT_S}s of polling"
                    )
                    raise Abort
                time.sleep(POLL_INTERVAL_S)
                tick = served_tick(duty_url, failures)
            evidence["severed_at"] = tick
        finally:
            client.close()
        deadline = time.monotonic() + BATCH_SETTLE_TIMEOUT_S
        while tick < target and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_S)
            tick = served_tick(duty_url, failures)
        if tick < target:
            failures.append(
                "the severed client's batch never terminated — the "
                f"served tick stands at {tick} past the settle "
                f"deadline, expected the batch's bound {target}"
            )
            raise Abort
        if tick > target:
            failures.append(
                f"the dead client's batch climbed to tick {tick}, "
                f"past its bound {target} — the run kept stepping on "
                "a client that was gone"
            )
            raise Abort
        # The batch holds its end: a driven run advances only for a
        # requesting client, so the quiet window is the termination
        # evidence — nothing keeps stepping on the dead client's
        # behalf once its request is spent.
        time.sleep(HOLD_SETTLE_S)
        held_tick = served_tick(duty_url, failures)
        if held_tick != target:
            failures.append(
                f"the run kept stepping past the dead client's batch "
                f"— the served tick moved {target}→{held_tick} after "
                "the batch's bound"
            )
            raise Abort
        plant = plant_tick(rig)
        if plant != plant_target:
            failures.append(
                f"the shared plant's step counter stands at {plant}, "
                f"expected {plant_target} — the severed batch's "
                "bounded steps did not land exactly its bound"
            )
            raise Abort
        status, snapshot = pair.request(
            f"{duty_url}/scan", {"scans": 1}
        )
        if status != 200 or snapshot.get("tick") != target + 1:
            failures.append(
                f"the bounded follow-up answered {status} "
                f"{snapshot if status != 200 else snapshot.get('tick')} "
                f"— expected 200 at tick {target + 1}; the abandoned "
                "batch's worker stayed pinned"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "severed",
                "batch": bound,
                "settled": target,
                "plant": plant_target,
                "follow_up": target + 1,
            }
        )

        # Phase 4 — the restore: tracking-first pair ticks adopt the
        # post-batch image onto the standby, the pair reporting its
        # launch roles back — the field owner `active`, the tracking
        # peer `standby`.
        restored = rig.converge(failures, count=RESTORE_TICKS)
        evidence["restored"] = restored["ticks"][-1]
        digest_entries.append(
            {
                "phase": "restored",
                "ticks": restored["ticks"],
                "duty_role": restored["duty_role"],
                "standby_role": restored["standby_role"],
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
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
        choices=["under-bound"],
        help="doctor the leg's bound record so its probe lands inside "
        "the monitor's real bound — the pass must fail naming the "
        "unrefused batch",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = scan_batch_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "scan-batch-bound: the doctored under-bound probe ran "
                "unrefused is the doctored case's proof — an "
                "inconclusive run offers it no evidence"
            )
            return 1
        eprint(f"scan-batch-bound: inconclusive — {inconclusive}")
        print(f"scan-batch-bound-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"scan-batch-bound: {line}")
        return 1
    for failure in failures:
        eprint(f"scan-batch-bound: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"scan-batch-bound: the {args.tamper} case passed "
                "silently — the leg never noticed the unrefused "
                "batch"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"scan-batch-bound-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the over-bound probe of "
        f"{evidence['refused']} scans refused by name without a tick, "
        "the severed client's at-bound batch terminating on its bound "
        "with the shared plant stepped the same, and the pair's "
        f"launch roles restored at tick {evidence['restored']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
