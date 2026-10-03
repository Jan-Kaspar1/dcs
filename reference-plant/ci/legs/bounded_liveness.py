#!/usr/bin/env python3
"""The bounded-liveness leg for the reference plant — the
consumer-side proof that the deployed pair's container-health reads
answer inside their declared bound while a wedged field connection
stalls the scan loop, the wedge itself reported through the served
`last_scan_age_ms` rather than by the liveness read queueing behind
it (WW-ENG-003, WW-OPS-003).

The consumer-boundary mirror of the rig's bounded-liveness leg —
the contract defect #1147 names and its fix establishes: `GET
/health` and `GET /role` serve the store's published liveness mirror
instead of fetching through the executor lock, so a scan parked
inside remote field I/O — the `docker pause` wedge, the driver's
socket held open but unanswered — cannot stall them for the whole
driver timeout. The platform pin is the fix's own regression; this
leg exercises the same contract on the customer-owned pair whose
container `HEALTHCHECK` depends on it. The run:

- converges the manifest-declared pair — the armed standby tracking,
  the field owner active — and gates the contract: each peer's
  `GET /health` must answer the `HealthReport` shape — `live`, the
  served role, the run tick, and a stamped `last_scan_age_ms` —
  and every read must answer inside the declared bound on the
  settled baseline; a release whose answer predates the shape
  reports `bounded-liveness-digest inconclusive`, never a failure;
- wedges the deployed plant connection: the plant container's pause
  — `SIGSTOP` on the spawned stand-in, the harness's stop/pause
  lever — holds the driver's sockets open but unanswered, so a
  driven `POST /scan` on each peer parks inside field I/O for the
  driver's whole timeout, holding the executor lock the pre-contract
  liveness fetch queued behind. Through the wedge the leg polls
  `GET /health` and `GET /role` beside the published-copy reads
  `GET /snapshot` and `GET /journal` on both controllers, each owed
  an answer inside the declared bound: a stalled liveness read is
  the pre-contract signature — inconclusive, the pinned release
  predating the contract — while `/snapshot` and `/journal` must
  keep serving their baseline and `/health` must keep reporting the
  wedge through a growing `last_scan_age_ms`;
- unpauses the plant: the parked scans complete, a quiet driven
  pair tick reconverges the peers, and the liveness reads answer
  bounded again with `last_scan_age_ms` re-stamped to scan-boundary
  freshness — the latency recovered — the pair standing in its
  launch roles.

Usage:

    bounded_liveness.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `bounded-liveness-digest <sha256>` line prints — the
check runs two passes and compares them
(`bounded-liveness-nondeterministic`). A contract violation reports
`bounded-liveness: …` lines on stderr and exits 1 — the check's
`bounded-liveness-failed`. `--tamper starved-reads` doctors the
declared answer bound to zero so every liveness read observes a
stall — the leg's bounded assertion must fire rather than pass an
unprobed contract.
"""

import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the liveness read answered
# inside the bound while it stalled — the declared bound doctored
# to zero — must surface the named diagnostic, never a silently
# unexercised contract.
LEG = {
    "order": 450,
    "title": "the bounded-liveness leg",
    "passes": "bounded-liveness-leg",
    "tampers": [
        {
            "name": "starved-reads",
            "passed": "a starved-reads case passed the bounded-liveness leg",
            "missed": "the starved-reads case did not report its named diagnostic",
            "evidence": ["never answered inside the declared"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the bounded-liveness contract the
    leg exercises — the liveness reads queueing behind the wedged
    scan's lock hold, or the `HealthReport` shape missing — or the
    consumer harness admits no stop/pause lever: the run classifies
    inconclusive, never a product failure."""


# The declared bound one liveness read owes — the "small bound" the
# contract names: the defect's rig reproduction rose from ~2ms mean
# to a ~4s stall behind the wedged scan's lock hold, so a second is
# far past every healthy latency and far short of the wedge the leg
# holds.
ANSWER_BOUND_S = 1.0

# The wedge's own pacing: the settle giving the driven scans their
# unanswered field requests before the probes, the spacing the
# growing `last_scan_age_ms` is read across — both far inside the
# driver's request timeout so the parked scans complete cleanly on
# the restore — and the bound joining them once the plant resumes.
WEDGE_SETTLE_S = 0.3
AGE_SPACING_S = 0.5
SCAN_JOIN_S = 20.0


def bounded_get(url, bound):
    """One bounded read — `(decoded, elapsed)` when the answer lands
    inside `bound` seconds, `(None, error)` when it stalls or
    errs."""
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=bound) as response:
            return json.load(response), time.monotonic() - started
    except Exception as error:
        return None, error


def health_report(body):
    """The decoded body when it carries the contract's HealthReport
    shape — `live`, `role`, `tick`, and the wedge-reporting
    `last_scan_age_ms` — else None: the pre-contract answer the
    inconclusive classification names."""
    if not isinstance(body, dict):
        return None
    if not all(
        key in body for key in ("live", "role", "tick", "last_scan_age_ms")
    ):
        return None
    return body


def drive_scan(url, results, name):
    """The driven `POST /scan` the wedge parks inside field I/O —
    its answer recorded for the restore's join."""
    try:
        results[name] = simulate.http(f"{url}/scan", {"scans": 1})
    except Exception as error:
        results[name] = error


def pause_plant(rig):
    """The deployed plant connection's wedge — the container's
    `docker pause` on the spawned stand-in: `SIGSTOP` holds the
    process so the driver's sockets stay open but unanswered, the
    harness's stop/pause lever beside `pair.stop`'s container stop.
    A lever that cannot land classifies inconclusive."""
    if not hasattr(signal, "SIGSTOP"):
        raise Inconclusive(
            "the consumer harness admits no stop/pause lever — no "
            "SIGSTOP"
        )
    try:
        rig.plant.send_signal(signal.SIGSTOP)
    except Exception as error:
        raise Inconclusive(
            f"the plant pause lever never landed: {error}"
        )


def resume_plant(rig):
    """The wedge's restore — `SIGCONT` so the buffered requests
    complete: the pause-then-unpause cycle the deployed `docker
    unpause` runs."""
    try:
        rig.plant.send_signal(signal.SIGCONT)
    except Exception:
        pass


def baseline_probe(url, name, expected, bound, failures):
    """The settled pair's bounded reads — the contract gate plus the
    published copies the wedge window holds to baseline. Returns the
    peer's baseline record."""
    body, seen = bounded_get(f"{url}/health", bound)
    if body is None:
        if isinstance(seen, urllib.error.HTTPError):
            raise Inconclusive(
                "the pinned release predates the bounded-liveness "
                f"contract — GET /health answers {seen.code}"
            )
        failures.append(
            f"GET /health on {name} never answered inside the "
            f"declared {bound}s bound on the settled baseline: {seen}"
        )
        return None
    report = health_report(body)
    if report is None:
        raise Inconclusive(
            "the pinned release predates the bounded-liveness "
            f"contract — GET /health answers {body}"
        )
    if not isinstance(report.get("last_scan_age_ms"), int):
        raise Inconclusive(
            "the pinned release's liveness answer stamps no "
            "last_scan_age_ms — the wedge-report field the contract "
            "names is absent from the converged pair's HealthReport"
        )
    if report.get("live") is not True or report.get("role") != expected:
        failures.append(
            f"{name}'s liveness answer serves {report} — expected "
            f"live with role {expected}"
        )
        return None
    role, seen = bounded_get(f"{url}/role", bound)
    if role is None:
        failures.append(
            f"GET /role on {name} never answered inside the "
            f"declared {bound}s bound on the settled baseline: {seen}"
        )
        return None
    if role.get("role") != expected:
        failures.append(
            f"{name} reports role {role.get('role')!r}, expected "
            f"{expected}"
        )
        return None
    for path in ("/snapshot", "/journal"):
        body, seen = bounded_get(f"{url}{path}", bound)
        if body is None:
            failures.append(
                f"GET {path} on {name} never answered inside the "
                f"declared {bound}s bound on the settled baseline: "
                f"{seen}"
            )
            return None
        if path == "/snapshot":
            snapshot = body
        else:
            journal = body
    return {
        "health": report,
        "role": role,
        "snapshot": snapshot,
        "journal": journal,
    }


def wedge_probe(url, name, expected, bound, baseline, failures):
    """One wedged-window probe round: the liveness reads owed an
    answer inside the declared bound while the scan loop stands
    parked — a stall here is the pre-contract signature, never a
    product failure — beside the published-copy reads held to
    baseline. Returns the round's normalized marks."""
    marks = {}
    report, seen = bounded_get(f"{url}/health", bound)
    if report is None:
        raise Inconclusive(
            f"GET /health on {name} stalled behind the wedged scan "
            "— the pinned release predates the bounded-liveness "
            f"contract: {seen}"
        )
    report = health_report(report)
    if report is None or not isinstance(
        report.get("last_scan_age_ms"), int
    ):
        failures.append(
            f"{name}'s liveness answer under the wedge serves no "
            "HealthReport — the contract's wedge-report shape is "
            "absent"
        )
        return marks
    if report.get("live") is not True or report.get("role") != expected:
        failures.append(
            f"{name}'s liveness answer under the wedge serves "
            f"{report} — expected live with role {expected}"
        )
        return marks
    marks["age"] = report["last_scan_age_ms"]
    role, seen = bounded_get(f"{url}/role", bound)
    if role is None:
        raise Inconclusive(
            f"GET /role on {name} stalled behind the wedged scan "
            "— the pinned release predates the bounded-liveness "
            f"contract: {seen}"
        )
    if role.get("role") != expected:
        failures.append(
            f"{name} reports role {role.get('role')!r} under the "
            f"wedge, expected {expected} — the field connection's "
            "stall moved a role"
        )
        return marks
    snapshot, seen = bounded_get(f"{url}/snapshot", bound)
    if snapshot is None:
        failures.append(
            f"GET /snapshot on {name} never answered inside the "
            f"declared {bound}s bound while the field connection "
            f"stood wedged: {seen}"
        )
        return marks
    if (
        pair.select_snapshot(snapshot)
        != pair.select_snapshot(baseline["snapshot"])
        or snapshot.get("tick") != baseline["snapshot"].get("tick")
    ):
        failures.append(
            f"{name}'s served snapshot moved off its baseline while "
            "the field connection stood wedged — the parked scan "
            "published where it should have stalled"
        )
        return marks
    marks["snapshot"] = "baseline"
    journal, seen = bounded_get(f"{url}/journal", bound)
    if journal is None:
        failures.append(
            f"GET /journal on {name} never answered inside the "
            f"declared {bound}s bound while the field connection "
            f"stood wedged: {seen}"
        )
        return marks
    if journal != baseline["journal"]:
        failures.append(
            f"{name}'s served journal moved off its baseline while "
            "the field connection stood wedged — the parked scan "
            "published where it should have stalled"
        )
        return marks
    marks["journal"] = "baseline"
    marks["liveness"] = "bounded"
    return marks


def bounded_liveness_pass(args, tamper):
    """The bounded-liveness run: converge the armed declared pair,
    gate the contract on the settled baseline, wedge the plant
    connection, poll the liveness and published-copy reads through
    the stall, then prove the latency recovers on the unpause.
    Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the
    contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "bounded-liveness leg has nothing to exercise"
        )
    _manifest, _duty_decl, standby_decl = declared
    budget = standby_decl.get("failover_budget")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        raise Abort(
            "the manifest's standby declares no positive "
            "failover_budget — the deployed pair's armed heartbeat "
            "is absent"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    paused = False
    try:
        rig = pair.launch_pair(args, declared, auto_promote=budget)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        peers = (
            (rig.duty_decl["name"], duty_url, "active"),
            (rig.standby_decl["name"], standby_url, "standby"),
        )
        # The `starved-reads` tamper doctors the declared bound to
        # zero — every read observes the stall a regressed liveness
        # surface would produce.
        bound = 0.0 if tamper == "starved-reads" else ANSWER_BOUND_S

        # Phase 1 — convergence: the manifest-declared pair settled,
        # the armed standby tracking and the field owner active.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": "active",
                "standby_role": "standby/tracking",
            }
        )

        # Phase 2 — the settled baseline: every read owed its answer
        # inside the declared bound with no wedge to excuse a stall,
        # the HealthReport shape gating the contract, and the
        # published copies recorded for the wedge window's hold.
        baseline = {}
        marks = {}
        for name, url, expected in peers:
            record = baseline_probe(url, name, expected, bound, failures)
            if record is None:
                continue
            baseline[name] = record
            marks[name] = {
                "role": record["role"]["role"],
                "tick": record["health"]["tick"],
            }
        if failures:
            raise Abort
        digest_entries.append({"phase": "baseline", "peers": marks})

        # Phase 3 — the wedge and its probes under a guaranteed
        # restore: pause the deployed plant connection, drive one
        # scan on each peer so the loop parks inside the unanswered
        # field request, then poll — the liveness reads inside the
        # declared bound beside the published copies held at
        # baseline, the wedge reported through the growing
        # `last_scan_age_ms`.
        results = {}
        threads = []
        wedge = {}
        ages = {}
        pause_plant(rig)
        paused = True
        try:
            for name, url, _expected in peers:
                thread = threading.Thread(
                    target=drive_scan, args=(url, results, name),
                    daemon=True,
                )
                thread.start()
                threads.append(thread)
            time.sleep(WEDGE_SETTLE_S)
            for name, url, expected in peers:
                marks = wedge_probe(
                    url, name, expected, bound, baseline[name], failures
                )
                ages[name] = marks.pop("age", None)
                wedge[name] = marks
            if failures:
                raise Abort
            time.sleep(AGE_SPACING_S)
            for name, url, _expected in peers:
                report, seen = bounded_get(f"{url}/health", bound)
                if report is None:
                    raise Inconclusive(
                        f"GET /health on {name} stalled behind the "
                        "wedged scan — the pinned release predates "
                        "the bounded-liveness contract: "
                        f"{seen}"
                    )
                report = health_report(report) or {}
                earlier = ages[name]
                later = report.get("last_scan_age_ms")
                if not isinstance(later, int) or not later > earlier:
                    failures.append(
                        f"{name}'s last_scan_age_ms did not grow "
                        "under the wedge — the liveness answer "
                        f"stopped reporting the stalled scan: "
                        f"{earlier}ms then {later}ms"
                    )
                else:
                    ages[name] = later
                    wedge[name]["scan_age"] = "grew"
            if failures:
                raise Abort
        finally:
            resume_plant(rig)
            paused = False
        digest_entries.append({"phase": "wedge", "peers": wedge})

        # Phase 4 — the restore: the parked scans answer once the
        # plant resumes, a quiet driven pair tick reconverges the
        # peers, and the liveness reads answer bounded again with the
        # mirror re-stamped — the latency recovered, the pair in its
        # launch roles.
        for thread in threads:
            thread.join(SCAN_JOIN_S)
        for name, _url, _expected in peers:
            answer = results.get(name)
            if not isinstance(answer, dict):
                failures.append(
                    f"the wedged POST /scan on {name} never answered "
                    f"after the restore: {answer}"
                )
                raise Abort
        _tracked, owner = rig.tick(standby_url, duty_url, failures)
        evidence["recovered"] = owner["tick"]
        restored = {}
        for name, url, expected in peers:
            report, seen = bounded_get(f"{url}/health", bound)
            if report is None:
                failures.append(
                    f"GET /health on {name} never answered inside "
                    f"the declared {bound}s bound after the "
                    f"restore: {seen}"
                )
                continue
            report = health_report(report) or {}
            recovered = report.get("last_scan_age_ms")
            if not isinstance(recovered, int) or recovered >= ages[
                name
            ]:
                failures.append(
                    f"{name}'s post-recovery scan never re-stamped "
                    "the liveness mirror — last_scan_age_ms reads "
                    f"{recovered}ms against the wedge's "
                    f"{ages[name]}ms"
                )
                continue
            if report.get("role") != expected:
                failures.append(
                    f"{name} reports role {report.get('role')!r} "
                    f"after the restore, expected {expected} — the "
                    "pair's launch roles moved"
                )
                continue
            restored[name] = {"scan_age": "restamped", "role": expected}
        standby_role = None
        for _ in range(3):
            standby_role = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            sync = standby_role.get("sync")
            if standby_role.get("role") == "standby" and (
                isinstance(sync, dict) and "tracking" in sync
            ):
                break
            _tracked, owner = rig.tick(standby_url, duty_url, failures)
        else:
            failures.append(
                "the tracking peer never reconverged after the "
                f"restore — GET /role answers {standby_role}"
            )
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r} "
                "after the restore, expected active — the pair's "
                "launch roles moved"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "recovery",
                "resumed_tick": owner["tick"],
                "peers": restored,
                "standby_role": "standby/tracking",
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if paused and rig is not None and rig.plant is not None:
            resume_plant(rig)
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
        choices=["starved-reads"],
        help="doctor the declared answer bound to zero — the pass "
        "must fail naming the stalled reads",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = bounded_liveness_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"bounded-liveness: the {args.tamper} case's reads "
                "never answered inside the declared bound — an "
                "inconclusive run offers the doctored case no "
                "evidence"
            )
            return 1
        eprint(f"bounded-liveness: inconclusive — {inconclusive}")
        print(f"bounded-liveness-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"bounded-liveness: {line}")
        return 1
    for failure in failures:
        eprint(f"bounded-liveness: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"bounded-liveness: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored bound"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"bounded-liveness-digest {digest} — tracking by tick "
        f"{evidence['converged']}, both peers' /health and /role "
        f"answered inside {ANSWER_BOUND_S}s while the field "
        "connection stood wedged — last_scan_age_ms growing, the "
        "published copies at baseline — latency recovered and "
        f"launch roles standing at tick {evidence['recovered']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
