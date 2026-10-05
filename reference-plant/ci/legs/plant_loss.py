#!/usr/bin/env python3
"""The plant-loss leg for the reference plant — the consumer-side
mirror of the rig's plant-link-loss scenario (`3500_plant_link_loss`,
scenario key `plant-link-loss`, requirements WW-ENG-003, WW-OPS-003
and WW-LCM-001): the customer-owned pair's field-loss degradation and
its fail-closed restart window, proven on the deployment
`deploy/manifest.json` actually declares rather than only on the rig.

No consumer leg stops the deployed pair's plant server: the consumers
stage replays reader schedules and the pair stage proves switchover,
but the link-loss degradation and the fail-closed restart window are
unproven on the customer boundary. This leg runs the episode on the
manifest-declared pair, ordered by the driven harness — a peer
exchanges with the field only inside its own `POST /scan`, so the
outage window is the harness's to hold open:

- converges the declared pair to `tracking` and gates the contract
  surface: the field's writer claim standing under the launched
  owner's own recorded owner token (read out of the controller's
  startup preamble), the declared durable journal files the audit
  reads, and the served `io_health` section — the counters and the
  backend driver diagnostics the outage's account is read from. A
  pinned release predating any of that is the pre-contract shape, and
  the leg reports `plant-loss-digest inconclusive` rather than
  asserting;
- stops the spawned `dcs-plant-server` — the honest container-side
  interruption a deployed plant container's `docker stop` is — and
  drives scans on the field owner through the outage: the run's
  cadence still advancing (the served tick never rewinding — a
  controller restart is not how a field owner rides a link loss out),
  the field reads re-marked at the link boundary, `io_health`
  counting the per-direction failures on both sides and still
  advancing, and the backend driver link reporting `disconnected`
  with a named `last_error` recorded. The declared standby's role
  never moves: field loss is not a promotion event, its
  checkpoint-pull heartbeat being peer-to-peer rather than field
  traffic;
- respawns the plant server as a new lifetime on the interrupted
  link's own address — a new process whose single-writer claim died
  with the old one, so the field returns **fail closed**: the
  mutation probes a dedicated plant-socket attachment runs answer the
  named `unclaimed` refusal — never `stepped`, never a silent write —
  until the recorded owner's bounded re-attach re-arms its claim
  through `ensure_writer`. The leg drives the owner's own scans across
  the window to land that re-arm, then proves it: the probes answer
  `fenced` again naming the recorded owner, the simulated plant's
  `Good` reads recover, and the outage's counted failures are still
  counted in `io_health` rather than silently reset. No controller
  restarts across the episode;
- leaves the pair's launch roles unchanged and the pair settled: the
  field owner `active`, the declared standby `tracking` it.

Usage:

    plant_loss.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `plant-loss-digest <sha256>` line prints — the check
runs two passes and compares them (`plant-loss-nondeterministic`). A
contract violation reports `plant-loss: …` lines on stderr and exits 1
— the check's `plant-loss-failed`. The `--tamper` cases doctor the
leg's own expectations, and each must fail carrying its named
evidence: `expect-owner-exit` wants the field owner's process to have
exited on the link loss, `expect-self-promotion` wants the standby to
have promoted itself, `expect-open-window` wants a mutation admitted
before the owner re-armed, `expect-reset-counters` wants the outage's
counted failures silently cleared, and `skip-outage` never stops the
plant but keeps the recovery assertions.
"""

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import driver_recovery
import failover
import pair
import simulate
import stranded_rejoin


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored cases.
LEG = {
    # The next free slot after standby-loss 880.
    "order": 890,
    "title": "the plant-loss leg",
    "passes": "plant-loss",
    "tampers": [
        {
            "name": "expect-owner-exit",
            "passed": "a doctored owner-exit expectation passed the plant-loss leg",
            "missed": "the expect-owner-exit case did not report its named diagnostic",
            "evidence": ["expected the field owner's process to have exited"],
        },
        {
            "name": "expect-self-promotion",
            "passed": "a doctored self-promotion expectation passed the plant-loss leg",
            "missed": "the expect-self-promotion case did not report its named diagnostic",
            "evidence": ["expected the standby to have promoted itself"],
        },
        {
            "name": "expect-open-window",
            "passed": "a doctored open-window expectation passed the plant-loss leg",
            "missed": "the expect-open-window case did not report its named diagnostic",
            "evidence": ["expected the restarted plant to admit a mutation"],
        },
        {
            "name": "expect-reset-counters",
            "passed": "a doctored counter expectation passed the plant-loss leg",
            "missed": "the expect-reset-counters case did not report its named diagnostic",
            "evidence": ["expected the outage's counted failures to be cleared"],
        },
        {
            "name": "skip-outage",
            "passed": "a skip-outage passed the plant-loss leg",
            "missed": "the skip-outage case did not report its named diagnostic",
            "evidence": ["expected the restart's fail-closed window"],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the contract the leg exercises — the
    run classifies inconclusive, never a product failure. The first arg
    is the stable reason the `inconclusive` digest line prints; the
    optional second arg is the run's own evidence, reported on stderr
    only."""


# The driven-scan bounds each phase gets: the outage's degraded serve,
# the window the owner needs to re-attach and re-arm its claim, and
# the settle train that closes the episode.
OUTAGE_SCANS = 6
REATTACH_SCANS = 8
STANDBY_LINK_SCANS = 10
FAIL_CLOSED_PROBES = 4
SETTLE_TICKS = 4
# The wall-clock the bounded lazy re-attach needs — the connection's
# one-second re-attach spacing plus margin.
REATTACH_WAIT = 1.2


def driver_health(snapshot):
    """The served io_health's backend driver diagnostics — the
    DriverDiagnostics the remote backend volunteers."""
    return driver_recovery.driver_health(snapshot)


def degraded(health, driver):
    """The interrupted link's degraded serve: the backend reports
    `disconnected` carrying a named standing failure, the boundary
    streak counts, and the fault is recorded."""
    return driver_recovery.degraded(health, driver)


def marked_down(qualities):
    """The read half of the degraded serve over the outage's served
    quality samples — `(still_good, named_bad)`: the samples still
    reading `good` behind an interrupted link, and whether the field's
    own reads reached a named `bad:<reason>` verdict. The link boundary
    marks the field down; a read still serving Good, or one that only
    ever went uncertain, is not the contract the rig's
    `3500_plant_link_loss` grades."""
    return (
        [sample for sample in qualities if sample == "good"],
        any(str(sample).startswith("bad:") for sample in qualities),
    )


def tracking(report):
    """Whether a served RoleReport reads `standby` under `tracking`
    sync."""
    sync = report.get("sync")
    return report.get("role") == "standby" and (
        isinstance(sync, dict) and "tracking" in sync
    )


def role(url, failures):
    """The peer's served RoleReport."""
    return pair.get(f"{url}/role", "GET /role", failures)


def outage_leg(rig, duty_url, standby_url, model_path, tamper, failures,
               evidence):
    """The degraded window: the plant server stops, the field owner is
    driven throughout, and its served account — cadence, read quality,
    `io_health` counters, backend diagnostics — plus the declared
    standby's unchanged role are read. Returns the outage record."""
    before = pair.get(f"{duty_url}/snapshot", "GET /snapshot", failures)
    health0 = before.get("io_health") or {}
    if not isinstance(health0, dict):
        raise Inconclusive(
            "the served snapshot carries no io_health section — the "
            "pinned release predates the field-loss contract the leg "
            "reads"
        )
    if driver_health(before) is None:
        raise Inconclusive(
            "the served io_health carries no backend driver "
            "diagnostics — the pinned release predates the "
            "remote-driver reporting the outage's account is read from"
        )
    stopped = tamper != "skip-outage"
    if stopped:
        try:
            pair.stop(rig.plant)
        except Exception as error:
            raise Inconclusive(
                f"the field-outage lever never completed: {error}"
            )
        rig.plant = None
        if rig.plant_io is not None:
            rig.plant_io.close()
            rig.plant_io = None
    point = _probe_point(model_path)
    window = {"stopped": stopped, "scans": [], "syncs": [],
              "qualities": [], "health": None, "driver": None,
              "promotions": []}
    degraded_at = None
    for _ in range(OUTAGE_SCANS):
        peer_role = role(standby_url, failures)
        window["syncs"].append(stranded_rejoin.sync_kind(peer_role))
        if tamper == "expect-self-promotion":
            # The doctored expectation: the declared standby promotes
            # itself on the field outage. The honest run never does, so
            # the assertion cannot stand.
            if peer_role.get("role") != "active":
                window["promotions"].append("stayed-standby")
                failures.append(
                    f"{TAMPER_SELF_PROMOTION} — GET /role answered "
                    f"{peer_role.get('role')!r} throughout the outage"
                )
        elif peer_role.get("role") != "standby":
            failures.append(
                f"the declared standby reports "
                f"{peer_role.get('role')!r} while the shared plant is "
                "down — field loss is not a promotion event: "
                f"{json.dumps(peer_role)[:300]}"
            )
            raise Abort
        owner = pair.scan(duty_url, failures)
        health = owner.get("io_health") or {}
        driver = driver_health(owner) or {}
        tick = owner.get("tick")
        window["scans"].append(tick)
        if isinstance(tick, int) and isinstance(evidence.get("peak"), int) \
                and tick < evidence["peak"]:
            failures.append(
                f"the field owner's served tick rewound to {tick} under "
                f"the running peak {evidence['peak']} — a controller "
                "restarted instead of riding the link loss out"
            )
            raise Abort
        if isinstance(tick, int):
            evidence["peak"] = max(evidence.get("peak", tick), tick)
        quality = None if point is None else served_quality(owner, point)
        if quality is not None:
            window["qualities"].append(quality)
        # The degraded serve is two-sided: the backend reports the
        # interrupted link, and the field's own reads are marked down
        # at that same boundary. The window closes on both — the
        # outage's evidence then carries the read quality the
        # recovery leg restores to Good.
        if degraded(health, driver) and quality != "good":
            degraded_at = (owner, dict(health), dict(driver))
            window["health"] = dict(health)
            window["driver"] = dict(driver)
            window["quality"] = quality
            break
    if not stopped:
        failures.append(
            "expected the restart's fail-closed window: the plant "
            "server was never stopped, so no unclaimed window stood "
            "between the outage and the re-arm"
        )
        raise Abort
    if degraded_at is None:
        failures.append(
            "the interrupted plant link never surfaced degraded on the "
            "field owner's served backend diagnostics — the last "
            f"served io_health reads {json.dumps(window['health'])[:400]}"
        )
        raise Abort
    owner, outage_health, outage_driver = degraded_at
    evidence["outage"] = {
        "failed_reads": outage_health.get("failed_reads"),
        "failed_writes": outage_health.get("failed_writes"),
        "consecutive_failures": outage_health.get("consecutive_failures"),
        "link": outage_driver.get("link"),
        "named": bool(outage_driver.get("last_error")),
    }
    evidence["outage_health"] = outage_health
    if (outage_health.get("failed_reads") or 0) <= (
        health0.get("failed_reads") or 0
    ):
        failures.append(
            "the interrupted link never counted a failed read: "
            f"{json.dumps(outage_health)[:300]}"
        )
    field_out = any(
        entry.get("direction") == "out"
        for entry in owner.get("points") or []
    )
    if field_out and (outage_health.get("failed_writes") or 0) <= (
        health0.get("failed_writes") or 0
    ):
        failures.append(
            "the interrupted link counted read failures but no write "
            "failures though the field serves output points: "
            f"{json.dumps(outage_health)[:300]}"
        )
    if not evidence["outage"]["named"]:
        failures.append(
            "the disconnected backend recorded no last_error: "
            f"{json.dumps(outage_driver)[:300]}"
        )
    # The link boundary marks the field's own reads down: the probed
    # field input reads a named bad verdict through the outage and
    # never keeps serving Good behind a disconnected link — the same
    # two-sided degraded contract the rig's `3500_plant_link_loss`
    # grades.
    if point is not None:
        still_good, named_bad = marked_down(window["qualities"])
        if still_good:
            failures.append(
                f"the field owner's served reads of point {point} kept "
                f"reading good through the outage: {still_good} — the "
                "link boundary must mark the field's reads down"
            )
        if not named_bad:
            failures.append(
                f"the field owner's served reads of point {point} never "
                f"reached a named bad verdict through the outage: "
                f"{window['qualities']}"
            )
    if failures:
        raise Abort
    if tamper == "expect-owner-exit":
        failures.append(
            f"{TAMPER_OWNER_EXIT} — the field owner's monitor kept "
            "answering throughout the outage"
        )
        raise Abort
    return {
        "stopped": True,
        "scans": window["scans"],
        "peer_sync": sorted(set(window["syncs"])),
        "link": outage_driver.get("link"),
        "named": True,
        "quality": window.get("quality"),
        "failed_reads": outage_health.get("failed_reads"),
        "failed_writes": outage_health.get("failed_writes"),
        "streak": outage_health.get("consecutive_failures"),
    }


def served_quality(snapshot, point):
    """One point's served quality out of a served `/snapshot`, as the
    comparable key the failover leg's own `quality_key` reads — `good`,
    `uncertain:<reason>`, `bad:<reason>`. The link-boundary read the
    outage degrades and the recovery restores."""
    return failover.quality_key(failover.sample(snapshot, point))


def _probe_point(model_path):
    """The channel-backed field input the leg's reads and mutation
    probes target — a point the plant server actually serves, an
    internal point carrying no channel being bound to nothing."""
    with open(model_path) as handle:
        model = json.load(handle)
    channeled = [
        point["id"]
        for point in model.get("io_points", [])
        if point.get("channel")
    ]
    return min(channeled) if channeled else None


def fail_closed_window(rig, owner_token, tamper, failures, evidence):
    """The restarted plant's fail-closed window: a dedicated
    plant-socket attachment's mutation probes must answer the named
    `unclaimed` refusal — never `stepped`, never a silent write — until
    the recorded owner's own scans re-arm the claim. Returns the
    window record."""
    probe_io = simulate.PlantClient(rig.plant_addr)
    try:
        window = {"probes": [], "kinds": [], "owners": []}
        for _ in range(FAIL_CLOSED_PROBES):
            probe = probe_io.request({"op": "step", "dt": 0})
            kind = failover.probe_kind(probe)
            window["probes"].append(probe)
            window["kinds"].append(kind)
            window["owners"].append(
                (probe.get("error") or {}).get("owner")
            )
            if tamper == "expect-open-window":
                if kind != "granted":
                    failures.append(
                        f"{TAMPER_OPEN_WINDOW} — the mutation probe "
                        f"answered {kind} while no claim stood"
                    )
                break
            if kind == "granted":
                failures.append(
                    "the restarted plant admitted a third-party "
                    f"mutation with no claim behind it: {probe} — the "
                    "field must fail closed until the recorded owner's "
                    "re-attach re-arms it"
                )
                raise Abort
            if kind != "unclaimed":
                if kind != "fenced":
                    failures.append(
                        "the restarted plant answered a mutation probe "
                        f"neither a grant nor a named verdict: {probe}"
                    )
                    raise Abort
            if kind == "unclaimed":
                # The recorded owner's remote driver re-attaches on its
                # own bounded spacing, not on the first exchange after
                # the listener returns — the same wait the
                # remote-driver recovery leg stages.
                time.sleep(REATTACH_WAIT)
            owner = pair.scan(rig.duty_url, failures)
            window.setdefault("rearm_scans", []).append(owner["tick"])
            probe_after = probe_io.request({"op": "step", "dt": 0})
            if failover.probe_kind(probe_after) == "fenced":
                window["rearmed"] = probe_after
                break
        if not window.get("rearmed"):
            failures.append(
                "the recorded owner's re-attach never re-armed the "
                "claim — the field stayed unclaimed behind a live "
                "owner: probes "
                + json.dumps(window["kinds"])
                + " over the owner's scans "
                + json.dumps(window.get("rearm_scans", []))
            )
            raise Abort
        owner_token_after = (window["rearmed"].get("error") or {}).get(
            "owner"
        )
        if owner_token_after != owner_token:
            failures.append(
                "the re-armed claim names "
                f"{owner_token_after}, not the recorded owner's own "
                f"pinned token {owner_token}: "
                f"{json.dumps(window['rearmed'])[:300]}"
            )
            raise Abort
        evidence["fail_closed"] = [str(kind) for kind in window["kinds"]]
        return {
            "kinds": [str(kind) for kind in window["kinds"]],
            "rearm_scans": window.get("rearm_scans", []),
            "rearmed": "owner",
        }
    finally:
        probe_io.close()


def recovery_leg(rig, duty_url, standby_url, outage_health, point,
                 tamper, failures, evidence):
    """The recovery: the owner's re-armed claim carries its own scans,
    the backend link reports `connected` with the standing record
    cleared, the outage's counted failures are still counted, and the
    pair reconverges to one active plus one tracking standby."""
    time.sleep(REATTACH_WAIT)
    recovered = None
    health = {}
    driver = {}
    for _ in range(REATTACH_SCANS):
        owner = pair.scan(duty_url, failures)
        health = owner.get("io_health") or {}
        driver = driver_health(owner) or {}
        if driver.get("link") == "connected":
            recovered = owner
            break
    if recovered is None:
        failures.append(
            "the field owner's backend never re-attached — the served "
            f"link still reports {driver.get('link')!r} while the "
            f"restarted plant answers: {json.dumps(health)[:400]}"
        )
        raise Abort
    if driver.get("last_error"):
        failures.append(
            "the backend's last_error lingered behind the restored "
            "link — the first successful exchange after the plant's "
            f"return did not clear the standing failure: "
            f"{str(driver.get('last_error'))[:300]}"
        )
        raise Abort
    if (health.get("consecutive_failures") or 0) > 0:
        failures.append(
            "the boundary failure streak still stood behind the "
            f"healthy link: {json.dumps(health)[:300]}"
        )
        raise Abort
    if tamper == "expect-reset-counters":
        if (health.get("failed_reads") or 0) >= (
            outage_health.get("failed_reads") or 0
        ):
            failures.append(
                f"{TAMPER_RESET_COUNTERS} — io_health counts "
                f"{health.get('failed_reads')} read failures where the "
                f"outage counted {outage_health.get('failed_reads')}"
            )
    elif ((health.get("failed_reads") or 0)
          < (outage_health.get("failed_reads") or 0)
          or (health.get("failed_writes") or 0)
          < (outage_health.get("failed_writes") or 0)):
        failures.append(
            "the outage's counted history reset across the recovery — "
            "the cumulative counters dropped what they counted: "
            f"{json.dumps(health)[:400]}"
        )
        raise Abort
    # The pair's resumption: the tracking peer's own backend rides the
    # same lazy re-attach, then one tracking-first tick proves the
    # images reconverged identical.
    standby_driver = {}
    for _ in range(STANDBY_LINK_SCANS):
        time.sleep(REATTACH_WAIT)
        tracked = pair.scan(standby_url, failures)
        standby_driver = driver_health(tracked) or {}
        if standby_driver.get("link") == "connected":
            break
    if standby_driver.get("link") != "connected":
        failures.append(
            "the tracking peer's backend never re-attached — its "
            f"served link still reports {standby_driver.get('link')!r} "
            "while the restarted plant answers"
        )
        raise Abort
    ticks = []
    for _ in range(SETTLE_TICKS):
        _tracked, owner = rig.tick(
            standby_url,
            duty_url,
            failures,
            diverged="the restored pair's images diverged at tick "
            "{tick} — the recovery was not bumpless",
        )
        ticks.append(owner["tick"])
    owner_role = role(duty_url, failures)
    standby_role = role(standby_url, failures)
    if owner_role.get("role") != "active":
        failures.append(
            f"the field owner reports {owner_role.get('role')!r} after "
            "the recovery, expected active"
        )
        raise Abort
    if not tracking(standby_role):
        failures.append(
            f"the declared standby did not return to tracking standby "
            f"— GET /role answers {standby_role}"
        )
        raise Abort
    # The field's own reads recover: the outage re-marked them at the
    # link boundary, and the re-armed claim must carry them back to Good
    # with the outage's counted failures still counted above.
    quality = None
    if point is not None:
        quality = served_quality(owner, point)
        if quality != "good":
            failures.append(
                f"the field's own read of point {point} served {quality} "
                "after the re-arm — the recovered link must restore the "
                "field's Good reads"
            )
            raise Abort
    evidence["final_tick"] = ticks[-1]
    evidence["quality"] = quality
    return {
        "ticks": ticks,
        "owner_role": owner_role.get("role"),
        "standby_role": standby_role.get("role"),
        "link": driver.get("link"),
        "failed_reads": health.get("failed_reads"),
        "failed_writes": health.get("failed_writes"),
        "quality": quality,
    }


# The doctored cases' stable evidence prefixes — every failure the
# tampered pass records carries one, so the check's negative case finds
# it whether the honest run held or a predating release offered the
# doctored case no evidence at all.
TAMPER_OWNER_EXIT = "expected the field owner's process to have exited"
TAMPER_SELF_PROMOTION = "expected the standby to have promoted itself"
TAMPER_OPEN_WINDOW = "expected the restarted plant to admit a mutation"
TAMPER_RESET_COUNTERS = "expected the outage's counted failures to be cleared"


def respawn_plant(args, rig):
    """The restore half of the field-outage lever: a new
    `dcs-plant-server` lifetime bound where the interrupted one served,
    so the field owner's bounded lazy re-attach finds the endpoint
    again. A lever that cannot land is inconclusive, never a product
    failure — the same seam the remote-driver recovery leg stages."""
    driver_recovery.respawn_plant(args, rig)


def plant_loss_pass(args, tamper):
    """The plant-loss run: converge and gate the contract surface, stop
    the plant server and prove the degraded-but-scanning account with
    the standby's unchanged role, respawn the plant and prove the
    fail-closed unclaimed window plus the recorded owner's re-arm, then
    prove recovery to Good reads with the outage's failures still
    counted and the pair back on its launch roles. Returns
    `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the plant-loss "
            "leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        if rig.duty_files.get("journal_file") is None or (
            rig.standby_files.get("journal_file") is None
        ):
            raise Inconclusive(
                "the manifest's pair declares no journal files — the "
                "durable half of the outage's audit is absent"
            )

        # Phase 1 — convergence on the released images and the contract
        # surface: the launched active's recorded claim token, the
        # served io_health ledger, and the healthy backend baseline the
        # outage runs from.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        owner_token = failover.owner_token(rig.duty_preamble)
        if owner_token is None:
            raise Inconclusive(
                "the launched active recorded no claim line — the "
                "pinned release claims only on promotion, predating "
                "the field-loss contract the leg reads"
            )
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"].get("role"),
                "standby_role": converged["standby_role"].get("role"),
                "owner": "named",
                "health": "present",
            }
        )

        # Phase 2 — the degraded window.
        outage = outage_leg(
            rig, duty_url, standby_url, args.model, tamper, failures,
            evidence,
        )
        evidence["outage_at"] = outage["failed_reads"]
        digest_entries.append({"phase": "outage", **outage})

        # Phase 3 — the fail-closed restart window and the recorded
        # owner's re-arm.
        respawn_plant(args, rig)
        fail_closed = fail_closed_window(
            rig, owner_token, tamper, failures, evidence
        )
        evidence["rearmed_at"] = (
            fail_closed["rearm_scans"][-1]
            if fail_closed["rearm_scans"] else None
        )
        digest_entries.append({"phase": "fail-closed", **fail_closed})

        # Phase 4 — recovery to Good reads under the re-armed claim,
        # with the outage's failures still counted and no controller
        # restarted.
        recovered = recovery_leg(
            rig, duty_url, standby_url, evidence["outage_health"],
            _probe_point(args.model), tamper, failures, evidence,
        )
        digest_entries.append({"phase": "recovered", **recovered})
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
        choices=[
            "expect-owner-exit",
            "expect-self-promotion",
            "expect-open-window",
            "expect-reset-counters",
            "skip-outage",
        ],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = plant_loss_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "plant-loss: the doctored expectation wanted a contract "
                "the pinned release does not carry — an inconclusive "
                "run offers it no evidence"
            )
            return 1
        eprint(f"plant-loss: inconclusive — {inconclusive}")
        print(f"plant-loss-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"plant-loss: {line}")
        return 1
    for failure in failures:
        eprint(f"plant-loss: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"plant-loss: the {args.tamper} case passed silently "
                "— the leg never noticed the doctored expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"plant-loss-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the outage counted "
        f"{evidence['outage_at']} failed reads with the link "
        "disconnected and a named last_error while the field owner "
        f"kept scanning, the restart's fail-closed window answered "
        f"{evidence['fail_closed']} until the owner's re-attach re-armed "
        f"it at tick {evidence['rearmed_at']}, run continued to tick "
        f"{evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
