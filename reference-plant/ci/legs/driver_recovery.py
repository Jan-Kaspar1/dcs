#!/usr/bin/env python3
"""The remote-driver recovery leg for the reference plant — the
consumer-side proof that the served I/O-health surface reports the
field link as it is, not the worst thing that ever happened to it:
interrupting the manifest-declared pair's field-owning controller at
its plant link mid-run must surface the severed backend as
`disconnected` with the failure named in `last_error` and io_health
counting the degraded boundary, and the first successful exchange
after the link's return must clear the recorded failure — the healthy
backend never reporting behind a stale record (WW-ENG-003,
WW-OPS-003).

The consumer-boundary mirror of #990's remote-driver recovery
contract — the platform-side acceptance leg lives at
qa_lane/scenarios/3550_remote_driver_recovery.py. The pair leg
(`ci/legs/pair.py`) proves the declared pair runs and switches; the
failover leg proves a dead peer's armed standby takes the field. This
leg proves the remote driver's standing-failure recovery on the same
declared deployment: the rig launches the released tooling exactly as
the pair legs do — `dcs-plant-server` serving the model and dynamics,
the two `dcs-controller --driven --remote` peers converged to
`active`/`tracking` with the manifest's declared `failover_budget`
armed — then twice interrupts and restores the field owner's plant
link. Each cycle:

- severs the link: the spawned `dcs-plant-server` process stops — the
  honest consumer-side interruption, the `docker stop` a deployed
  plant container is — and driven tracking-first ticks run until the
  field owner's served snapshot reports the backend degraded: the
  driver diagnostics `disconnected` carrying the named `last_error`,
  io_health's consecutive-failure streak counting and the boundary
  fault recorded, the run's cadence still advancing and both peers'
  roles holding — the standby's checkpoint heartbeat is peer-to-peer,
  never field traffic, so field loss is not a promotion;
- restores the link: a new `dcs-plant-server` lifetime bound where
  the interrupted one served, so the driver's bounded lazy re-attach
  finds the endpoint again — then the first serve reporting the link
  `connected` must already carry the cleared record: the backend's
  `last_error` gone, the boundary streak reset, while the cumulative
  per-direction counters and the recorded fault keep the outage's
  history;
- records the normalized cycle marks; the second staged interruption
  must reproduce the first's verdicts exactly — two clean cycles
  produce identical digests.

A served `io_health` carrying no backend driver diagnostics, a link
that never presented the healthy baseline, or a staging lever that
cannot land — the severed endpoint never refusing, the restored plant
never serving the interrupted address — is the pinned release
predating the contract or the rig failing its precondition: the leg
reports `driver-recovery-inconclusive`, never a product failure.

Usage:

    driver_recovery.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `driver-recovery-digest <sha256>` line prints — the
check runs two passes and compares them
(`driver-recovery-nondeterministic`). A contract violation reports
`driver-recovery: …` lines on stderr and exits 1 — the check's
`driver-recovery-failed`. `--tamper expect-lingered` doctors the
leg's clearing verdict — asserting the recorded failure still stands
behind the restored link — so the leg proves its first-exchange
assertion fires rather than passing an unexercised contract.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the recorded failure lingers
# behind the restored link must surface the named diagnostic on the
# honest first-exchange clearing — never a silently unexercised
# contract.
LEG = {
    "order": 330,
    "title": "the remote-driver recovery leg",
    "passes": "driver-recovery-leg",
    "tampers": [
        {
            "name": "expect-lingered",
            "passed": "a expect-lingered case passed the driver-recovery leg",
            "missed": "the expect-lingered case did not report its named diagnostic",
            "evidence": ["expected the named last_error to linger"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the contract the leg exercises, or
    the link-staging lever never landed — the run classifies
    inconclusive, never a product failure."""


# The driven-tick bounds each half of a cycle gets: the outage's
# degraded serve and the restore's re-attached one. The wall-clock
# wait the restore gives the remote driver's bounded re-attach — the
# connection's one-second re-attach spacing plus margin.
OUTAGE_TICKS = 6
RESTORE_TICKS = 8
STANDBY_LINK_TICKS = 8
REATTACH_WAIT = 1.2


def driver_health(snapshot):
    """The served io_health's backend driver diagnostics — the
    DriverDiagnostics object the remote backend volunteers — or None
    when the snapshot carries no reporting backend."""
    health = (snapshot or {}).get("io_health") or {}
    driver = health.get("driver")
    return driver if isinstance(driver, dict) else None


def degraded(health, driver):
    """The interrupted link's degraded serve: the backend diagnostics
    report `disconnected` carrying the named standing failure, the
    boundary streak counts, and the fault is recorded."""
    return (
        driver.get("link") == "disconnected"
        and bool(driver.get("last_error"))
        and (health.get("consecutive_failures") or 0) > 0
        and bool(health.get("last_error"))
    )


def roles_hold(rig, failures, when):
    """One launch-roles poll mid-cycle — the field owner `active`, the
    tracking peer `standby`/`tracking`. A moved role is the pair's
    stability contract breaking inside the staged window: field loss
    is not a promotion, the armed budget's heartbeat is peer-to-peer.
    Returns False when a role moved."""
    held = True
    duty_role = pair.get(f"{rig.duty_url}/role", "GET /role", failures)
    if duty_role.get("role") != "active":
        failures.append(
            f"the field owner reports role {duty_role.get('role')!r} "
            f"{when} — the pair's launch roles did not hold: "
            f"{json.dumps(duty_role)[:300]}"
        )
        held = False
    standby_role = pair.get(
        f"{rig.standby_url}/role", "GET /role", failures
    )
    sync = standby_role.get("sync")
    if standby_role.get("role") != "standby" or not (
        isinstance(sync, dict) and "tracking" in sync
    ):
        failures.append(
            f"the tracking peer reports "
            f"{standby_role.get('role')!r}/{sync} {when} — the "
            "pair's launch roles did not hold: "
            f"{json.dumps(standby_role)[:300]}"
        )
        held = False
    return held


def respawn_plant(args, rig):
    """The restore half of the link-staging lever: a new
    `dcs-plant-server` lifetime bound where the interrupted one
    served, so the field owner's lazy re-attach finds the endpoint
    again. A lever that cannot land — the bind refused, the reported
    address moved, the server never answering — is inconclusive,
    never a product failure."""
    process = subprocess.Popen(
        [
            args.plant_server,
            args.model,
            "--dynamics",
            args.dynamics,
            "--listen",
            rig.plant_addr,
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        address = simulate.listen_address(process, "dcs-plant-server")
    except Exception as error:
        pair.stop(process)
        raise Inconclusive(
            "the link-staging restore never landed — the restarted "
            f"plant never reported a listener: {error}"
        )
    if address != rig.plant_addr:
        pair.stop(process)
        raise Inconclusive(
            f"the restarted plant bound {address}, not the "
            f"interrupted link's {rig.plant_addr} — the restore "
            "lever cannot land the endpoint"
        )
    rig.plant = process
    try:
        client = simulate.PlantClient(address)
        client.request({"op": "list_points"})
    except Exception as error:
        raise Inconclusive(
            "the restarted plant never served again — the "
            f"link-staging restore never landed: {error}"
        )
    rig.plant_io = client


def recovery_cycle(args, rig, number, tamper, state, failures):
    """One interrupt-and-restore of the field owner's plant link.
    `state` carries the run's peak served tick across cycles — a
    rewind is a controller restart, the nondeterminism diagnostic's
    signature. Returns the cycle's normalized marks — the verdict
    words two clean cycles produce identically."""
    marks = {
        "interrupted": None,
        "degraded": None,
        "cleared": None,
        "streak": None,
        "history": None,
        "roles": "held",
        "cadence": None,
    }
    duty_url, standby_url = rig.duty_url, rig.standby_url

    def watch_tick(snapshot):
        """Served-tick monotonicity across the cycle: a rewind is the
        tick-domain signature of a controller restarting instead of
        riding the link loss out."""
        tick = (snapshot or {}).get("tick")
        if not isinstance(tick, int):
            return
        peak = state.get("peak")
        if peak is not None and tick < peak:
            marks["cadence"] = "rewound"
            failures.append(
                "driver-recovery-nondeterministic: the field owner's "
                f"served tick rewound through cycle {number} — "
                f"{tick} under the running peak {peak}: a controller "
                "restarted instead of riding the link loss out"
            )
            raise Abort
        state["peak"] = tick if peak is None else max(peak, tick)

    tick0 = state.get("peak")

    # The interruption: the deployed plant container's `stop` — the
    # spawned server process stops mid-run, severing the field
    # owner's remote-driver link (and the tracking peer's — both
    # attach the same field). A stop lever that never completes is
    # inconclusive.
    try:
        pair.stop(rig.plant)
    except Exception as error:
        raise Inconclusive(
            f"the link-staging stop lever never completed: {error}"
        )
    rig.plant = None
    if rig.plant_io is not None:
        rig.plant_io.close()
        rig.plant_io = None
    marks["interrupted"] = "severed"

    # The degraded window: driven tracking-first pair ticks until the
    # field owner's served backend diagnostics report the outage —
    # or the bound runs out. Both peers' drivers fail identically, so
    # the pair's identical-image invariant holds through the window.
    outage = None
    health, driver = {}, {}
    for _ in range(OUTAGE_TICKS):
        if not roles_hold(rig, failures, "through the outage"):
            marks["roles"] = "moved"
            raise Abort
        _tracked, owner = rig.tick(standby_url, duty_url, failures)
        watch_tick(owner)
        health = owner.get("io_health") or {}
        driver = driver_health(owner) or {}
        if degraded(health, driver):
            outage = (owner, dict(health))
            break
    if outage is None:
        failures.append(
            "the interrupted plant link never surfaced degraded on "
            "the field owner's served backend diagnostics — the "
            f"last served io_health reads {json.dumps(health)[:400]}"
        )
        raise Abort
    owner, outage_health = outage
    marks["degraded"] = "link-down-named-counted"
    outage_reads = outage_health.get("failed_reads") or 0
    outage_writes = outage_health.get("failed_writes") or 0
    field_out = any(
        entry.get("direction") == "out"
        for entry in owner.get("points") or []
    )
    if outage_reads == 0:
        failures.append(
            "the interrupted link never counted a failed read: "
            f"{json.dumps(outage_health)[:300]}"
        )
    if field_out and outage_writes == 0:
        failures.append(
            "the interrupted link counted read failures but no "
            "write failures though the field serves output points: "
            f"{json.dumps(outage_health)[:300]}"
        )
    if failures:
        raise Abort

    # The restore: the plant returns as a new server lifetime on the
    # interrupted link's own address; the driver's lazy re-attach
    # lands the first successful exchange — which is also the
    # standing record's clearing boundary, so the first serve
    # reporting the link connected must already carry no last_error.
    # The duty scans alone through this window: the tracking peer's
    # re-attach is its own driver's business, and a one-driver lag
    # would read the peers' honestly different health as a diverged
    # image — the resumed parity tick below is the pair's check.
    respawn_plant(args, rig)
    time.sleep(REATTACH_WAIT)

    recovered = None
    for _ in range(RESTORE_TICKS):
        if not roles_hold(rig, failures, "across the plant's return"):
            marks["roles"] = "moved"
            raise Abort
        owner = pair.scan(duty_url, failures)
        watch_tick(owner)
        health = owner.get("io_health") or {}
        driver = driver_health(owner) or {}
        if driver.get("link") == "connected":
            recovered = (owner, health, driver)
            break
    if recovered is None:
        failures.append(
            "the field owner's backend never re-attached — the "
            f"served link still reports {driver.get('link')!r} "
            "while the restored plant answers: "
            f"{json.dumps(health)[:400]}"
        )
        raise Abort
    owner, health, driver = recovered
    marks["cleared"] = "re-attached"

    # The clearing verdict on the first connected serve — the
    # recorded failure must be gone already, never lingering behind
    # the now-healthy backend; the streak stands reset while the
    # cumulative counters and the recorded fault keep the outage's
    # history. The `expect-lingered` tamper doctors this verdict:
    # expecting the record still standing must fail on the honest
    # first-exchange clearing.
    if tamper == "expect-lingered":
        if not driver.get("last_error"):
            failures.append(
                "the restored link's first successful exchange "
                "cleared the standing record — expected the named "
                "last_error to linger behind the healthy backend"
            )
    elif driver.get("last_error"):
        failures.append(
            "the backend's last_error lingered behind the restored "
            "link — the first successful exchange after the "
            "plant's return did not clear the standing failure: "
            f"{str(driver.get('last_error'))[:300]}"
        )
    else:
        marks["cleared"] = "first-exchange"
    if (health.get("consecutive_failures") or 0) > 0:
        failures.append(
            "the boundary failure streak still stood behind the "
            f"healthy link: {json.dumps(health)[:300]}"
        )
    else:
        marks["streak"] = "reset"
    if (
        (health.get("failed_reads") or 0) < outage_reads
        or (health.get("failed_writes") or 0) < outage_writes
        or not health.get("last_error")
    ):
        failures.append(
            "the outage's counted history reset across the "
            "recovery — the cumulative counters or the recorded "
            "fault dropped what they counted: "
            f"{json.dumps(health)[:400]}"
        )
    else:
        marks["history"] = "retained"
    if (owner.get("tick") or 0) > (tick0 or 0):
        marks["cadence"] = "advancing"
    elif marks["cadence"] != "rewound":
        marks["cadence"] = "stalled"
        failures.append(
            "the served tick did not advance across the staged "
            f"cycle — the scan cadence held at {owner.get('tick')}"
        )
    if failures:
        raise Abort

    # The pair's resumption: the tracking peer's own backend rides
    # the same lazy re-attach — scan it standalone until its served
    # diagnostics report connected — then one tracking-first tick
    # proves the images reconverged identical.
    standby_driver = {}
    for _ in range(STANDBY_LINK_TICKS):
        tracked = pair.scan(standby_url, failures)
        standby_driver = driver_health(tracked) or {}
        if standby_driver.get("link") == "connected":
            break
    if standby_driver.get("link") != "connected":
        failures.append(
            "the tracking peer's backend never re-attached — its "
            f"served link still reports "
            f"{standby_driver.get('link')!r} while the restored "
            "plant answers"
        )
        raise Abort
    _tracked, owner = rig.tick(standby_url, duty_url, failures)
    watch_tick(owner)
    if not roles_hold(rig, failures, "closing the cycle"):
        marks["roles"] = "moved"
        raise Abort
    return marks


def driver_recovery_pass(args, tamper):
    """The recovery run: converge the armed declared pair, then two
    staged interruption-and-restore cycles against the field owner's
    plant link. Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "driver-recovery leg has nothing to exercise"
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
    try:
        rig = pair.launch_pair(args, declared, auto_promote=budget)
        duty_url = rig.duty_url

        # Phase 1 — convergence, then the baseline: the settled
        # healthy serve the interruptions run from — the backend's
        # connected link carrying no standing record. A snapshot
        # carrying no driver section is the release predating the
        # contract; a standing last_error behind a healthy link is
        # the contract's miss caught before the staging runs.
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
        baseline = converged["owner"]
        health = baseline.get("io_health")
        driver = driver_health(baseline)
        if not isinstance(health, dict) or driver is None or (
            "link" not in driver or "last_error" not in driver
        ):
            raise Inconclusive(
                "the served io_health carries no backend driver "
                "diagnostics — the pinned release predates the "
                "remote-driver recovery contract"
            )
        if driver.get("link") != "connected":
            raise Inconclusive(
                f"the backend link reports {driver.get('link')!r} "
                "ahead of the staged interruptions — the pair "
                "never presented the healthy baseline"
            )
        if driver.get("last_error"):
            failures.append(
                "the served backend diagnostics carry a standing "
                "last_error behind a healthy link ahead of the "
                "staged interruptions — the record never cleared "
                "on the exchanges since it stood: "
                f"{str(driver.get('last_error'))[:300]}"
            )
            raise Abort
        state = {"peak": baseline.get("tick")}

        # Phase 2 — the two staged cycles, pinned to identical
        # verdicts.
        marks = []
        for number in (1, 2):
            cycle = recovery_cycle(
                args, rig, number, tamper, state, failures
            )
            marks.append(cycle)
            digest_entries.append(
                {"phase": f"cycle-{number}", "marks": cycle}
            )
        if marks[0] != marks[1]:
            failures.append(
                "driver-recovery-nondeterministic: the two staged "
                "cycles' digests diverged: "
                f"{json.dumps(marks[0], sort_keys=True)} vs "
                f"{json.dumps(marks[1], sort_keys=True)}"
            )
            raise Abort

        # Phase 3 — the pair stands on its launch roles for the
        # next leg.
        if not roles_hold(rig, failures, "after the staged cycles"):
            raise Abort
        digest_entries.append(
            {
                "phase": "roles",
                "duty_role": "active",
                "standby_role": "standby/tracking",
            }
        )
    except Inconclusive:
        raise
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
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
        choices=["expect-lingered"],
        help="doctor the leg's clearing verdict — the pass must fail "
        "naming the honest first-exchange clearing",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = driver_recovery_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "driver-recovery: the doctored expectation wanted a "
                "lingering record — an inconclusive run offers the "
                "doctored case no evidence"
            )
            return 1
        eprint(f"driver-recovery: inconclusive — {inconclusive}")
        print(f"driver-recovery-inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"driver-recovery: {line}")
        return 1
    for failure in failures:
        eprint(f"driver-recovery: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"driver-recovery: the {args.tamper} case passed "
                "silently — the leg never noticed the honest "
                "clearing"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"driver-recovery-digest {digest} — tracking at tick "
        f"{evidence['converged']}, two staged interruptions severed "
        "the field owner's link to degraded-and-named, each restore "
        "cleared the standing record on the first exchange, roles "
        "and cadence held"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
