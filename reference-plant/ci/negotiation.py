#!/usr/bin/env python3
"""The checkpoint-negotiation leg for the reference plant — the
consumer-side proof that a standby pointed at the wrong model degrades
honestly (WW-ENG-003, WW-LCM-001's named-rejection-of-incompatible-
state clause), run entirely on the released tooling.

The platform's own tests prove the negotiation: a standby pulling a
checkpoint whose `model_fingerprint` it cannot adopt reports the named
degraded state and refuses promotion. This leg proves the *deployed*
contract the same way the pair leg proves the declared pair — the
misconfiguration a customer writing their own manifests can produce:
a third released `dcs-controller` launched `--standby <active>` on a
foreign document — the emitted model doctored inside `MODEL_VERSION`
to a fingerprint the running pair does not serve, launched without
`--revised`. The run:

- converges the manifest-declared pair to `tracking` first — the same
  wiring `ci/pair.py` runs — and captures the field owner's receipt
  log, journal position, and model fingerprint as the baseline the
  attempt must leave untouched;
- launches the foreign peer — `dcs-controller <doctored> --remote …
  --driven --standby <duty>` — and drives the observation window
  through `POST /scan`: every pull meets the fingerprint gate, so
  `GET /role` reports `standby` + `degraded` carrying the named
  fingerprint mismatch — the pair's fingerprint as the found half, the
  foreign document's as the expected — while the declared pair's
  snapshots stay identical and the field owner's tick advances;
- submits `POST /promote` on the foreign peer: the answer must be the
  named refusal — `409 not_converged` carrying the degraded
  negotiation state — never a silent or wrong verdict, and the refused
  request leaves the peer's reported state untouched;
- verifies the active undisturbed across the whole attempt: the
  receipt log identical to the baseline and the served journal's
  window additions carrying no role, fencing, or divergence records —
  the refused pulls never touched the field owner's record;
- tears the foreign peer down and proves the original pair still
  converges — later legs see the same rig;
- then runs the control leg: the same third controller launched on the
  pair's *own* model converges to `tracking` and `POST /promote`
  answers `promoting`, settling `active` at its next scan — the proof
  the refusal names the negotiation failure, not a rig defect.

Usage:

    negotiation.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `negotiation-digest <sha256>` line prints — the check
runs two passes and compares them (`negotiation-nondeterministic`). A
contract violation reports `negotiation: …` lines on stderr and exits
1 — the check's `negotiation-failed`. `--tamper expect-tracking`
flips the observation window's assertion to require convergence, so
the check can prove the leg's diagnostics fire — a tampered pass must
exit nonzero carrying the degraded report it actually saw.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pair
import simulate


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each phase runs: the declared pair's convergence
# before the foreign peer launches, the observation window over the
# refused peer, and the control peer's convergence — mirroring the
# pair leg's own phase bounds.
CONVERGE_TICKS = 4
WINDOW_TICKS = 4
CONTROL_TICKS = 4

# The journal event kinds the field owner's record must not gain while
# a foreign peer's pulls are refused — the attempt leaves no role,
# fencing, divergence, restart, or command evidence behind. Ordinary
# scan transitions (quality, journaled points, kind emissions) are the
# run's own record and are not disturbance.
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


def stop(process):
    """Terminate a spawned child, escalating to kill if it lingers."""
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def persistence(scratch, entry):
    """The controller's declared persistence file basenames
    instantiated under the leg's runner-owned scratch directory — the
    same substitution the pair leg performs."""
    root = os.path.join(scratch, entry["name"])
    os.makedirs(root, exist_ok=True)
    return {
        field: os.path.join(root, os.path.basename(entry[field]))
        if entry.get(field)
        else None
        for field in ("state_file", "journal_file")
    }


def foreign_document(model_path, scratch):
    """The foreign model document: the emitted model doctored inside
    `MODEL_VERSION` — the primary level measurement's declared
    description changes — so the parsed model still loads and
    assembles while its fingerprint differs from the pair's. Written
    into the leg's scratch directory; the checked-in model stays
    pristine."""
    with open(model_path) as handle:
        document = json.load(handle)
    signal = next(
        signal
        for signal in document["signals"]
        if signal["name"] == "level-primary"
    )
    signal["description"] = signal["description"] + " — foreign revision"
    path = os.path.join(scratch, "foreign-model.json")
    with open(path, "w") as handle:
        json.dump(document, handle, indent=2)
    return path


def foreign_role(url, pair_fingerprint, tamper, failures):
    """The observation window's per-poll assertion on the foreign
    peer: it must report `standby` + `degraded`, the detail naming the
    fingerprint negotiation — the pair's fingerprint the checkpoints
    carry. Under the `expect-tracking` tamper the leg instead requires
    convergence, so the check proves the assertion fires."""
    role = pair.get(f"{url}/role", "GET /role", failures)
    sync = role.get("sync")
    if tamper == "expect-tracking":
        if not (isinstance(sync, dict) and "tracking" in sync):
            failures.append(
                "the foreign peer never reported tracking — GET /role "
                f"answers {role}"
            )
            raise Abort
        return role
    detail = (
        sync.get("degraded", {}).get("detail", "")
        if isinstance(sync, dict)
        else ""
    )
    if role.get("role") != "standby" or not (
        isinstance(sync, dict) and "degraded" in sync
    ):
        failures.append(
            "the foreign peer left the refused negotiation state — "
            f"GET /role answers {role}, expected standby + degraded"
        )
        raise Abort
    if "fingerprint" not in detail or pair_fingerprint not in detail:
        failures.append(
            "the degraded negotiation state does not name the "
            f"fingerprint mismatch {pair_fingerprint}: {detail}"
        )
        raise Abort
    return role


def negotiation_pass(args, tamper):
    """The negotiation run: converge, foreign window, refusal,
    undisturbed, teardown, control. Returns `(digest_entries,
    evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the negotiation "
            "leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    scratch = tempfile.mkdtemp(prefix="dcs-negotiation-")
    digest_entries, evidence, failures = [], {}, []
    plant = duty = standby = foreign = control = None
    try:
        duty_files = persistence(scratch, duty_decl)
        standby_files = persistence(scratch, standby_decl)

        plant = subprocess.Popen(
            [
                args.plant_server,
                args.model,
                "--dynamics",
                args.dynamics,
                "--listen",
                "127.0.0.1:0",
            ],
            stderr=subprocess.PIPE,
            text=True,
        )
        plant_addr = simulate.listen_address(plant, "dcs-plant-server")

        duty, duty_url, preamble = pair.spawn_peer(
            args.controller, args.model, args.dt, plant_addr, None, duty_files
        )
        if duty_url is None:
            raise Abort(
                f"the duty controller {duty_decl['name']} exited at "
                f"startup: {'; '.join(preamble) or 'no diagnostic'}"
            )
        target = duty_url.removeprefix("http://")
        standby, standby_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            plant_addr,
            target,
            standby_files,
        )
        if standby_url is None:
            raise Abort(
                f"the standby controller {standby_decl['name']} exited "
                f"at startup: {'; '.join(preamble) or 'no diagnostic'}"
            )

        # Phase 1 — the declared pair converges and the baseline the
        # attempt must leave untouched is captured: the field owner's
        # model fingerprint (what its checkpoints carry), its receipt
        # log, and its journal position.
        ticks = []
        for _ in range(CONVERGE_TICKS):
            tracked = pair.scan(standby_url, failures)
            owner = pair.scan(duty_url, failures)
            if pair.select_snapshot(tracked) != pair.select_snapshot(owner):
                failures.append(
                    "the tracking peer's image diverged from the field "
                    f"owner's at tick {owner['tick']}"
                )
                raise Abort
            ticks.append(owner["tick"])
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the declared standby never reported tracking — GET "
                f"/role answers {standby_role}"
            )
            raise Abort
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r}, "
                "expected active"
            )
            raise Abort
        pair_checkpoint = pair.get(
            f"{duty_url}/checkpoint", "GET /checkpoint", failures
        )
        pair_fingerprint = format(pair_checkpoint["model_fingerprint"], "016x")
        receipts0 = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
        journal0 = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": ticks,
                "duty_role": duty_role,
                "standby_role": standby_role,
                "baseline": {
                    "model_fingerprint": pair_fingerprint,
                    "receipts": receipts0,
                    "journal_entries": len(journal0),
                },
            }
        )

        # Phase 2 — the foreign peer: a third released controller
        # launched --standby <duty> on the doctored document, without
        # --revised — the misconfigured deployment. Its own checkpoint
        # fingerprint is captured so the refusal's both halves are
        # named.
        foreign_doc = foreign_document(args.model, scratch)
        foreign, foreign_url, preamble = pair.spawn_peer(
            args.controller, foreign_doc, args.dt, plant_addr, target, {}
        )
        if foreign_url is None:
            raise Abort(
                "the foreign-model standby exited at startup: "
                f"{'; '.join(preamble) or 'no diagnostic'}"
            )
        foreign_checkpoint = pair.get(
            f"{foreign_url}/checkpoint", "GET /checkpoint", failures
        )
        foreign_fingerprint = format(
            foreign_checkpoint["model_fingerprint"], "016x"
        )
        if foreign_fingerprint == pair_fingerprint:
            failures.append(
                "the doctored document fingerprints identically to the "
                "pair's model — the leg's foreign deployment is not "
                "foreign"
            )
            raise Abort

        # Phase 3 — the observation window: each round drives the
        # foreign peer's refused pull, then the declared pair's scans.
        # The foreign peer must report standby + degraded naming the
        # fingerprint negotiation every round, while the pair's images
        # stay identical and the owner's tick advances.
        window = []
        previous = owner["tick"]
        for _ in range(WINDOW_TICKS):
            pair.scan(foreign_url, failures)
            tracked = pair.scan(standby_url, failures)
            owner = pair.scan(duty_url, failures)
            if pair.select_snapshot(tracked) != pair.select_snapshot(owner):
                failures.append(
                    "the tracking peer's image diverged from the field "
                    f"owner's at tick {owner['tick']} — the foreign "
                    "peer's refused pulls disturbed the pair"
                )
                raise Abort
            if owner["tick"] != previous + 1:
                failures.append(
                    f"the field owner's tick stalled at {owner['tick']} "
                    "during the foreign peer's window"
                )
                raise Abort
            previous = owner["tick"]
            window.append(
                {
                    "tick": owner["tick"],
                    "foreign_role": foreign_role(
                        foreign_url, pair_fingerprint, tamper, failures
                    ),
                }
            )
        detail = window[-1]["foreign_role"]["sync"]["degraded"]["detail"]
        if foreign_fingerprint not in detail:
            failures.append(
                "the degraded negotiation state does not name the "
                f"foreign document's own fingerprint "
                f"{foreign_fingerprint}: {detail}"
            )
            raise Abort
        evidence["degraded_detail"] = detail
        digest_entries.append(
            {
                "phase": "foreign",
                "foreign_fingerprint": foreign_fingerprint,
                "window": window,
            }
        )

        # Phase 4 — the promotion gate: POST /promote against the
        # never-converged peer must answer the named refusal, and the
        # refused request must leave its reported state untouched.
        status, refusal = pair.request(f"{foreign_url}/promote", {})
        refused_sync = (
            refusal.get("not_converged", {}).get("sync")
            if isinstance(refusal, dict)
            else None
        )
        if status != 409 or not (
            isinstance(refused_sync, dict) and "degraded" in refused_sync
        ):
            failures.append(
                "POST /promote on the foreign peer answered "
                f"{status} {refusal}, expected 409 not_converged "
                "carrying the degraded negotiation state"
            )
            raise Abort
        settled = pair.get(f"{foreign_url}/role", "GET /role", failures)
        if settled != window[-1]["foreign_role"]:
            failures.append(
                "the refused promotion changed the foreign peer's "
                f"reported state: {settled}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "refusal",
                "status": status,
                "refusal": refusal,
                "settled_role": settled,
            }
        )

        # Phase 5 — the active undisturbed: the receipt log exactly the
        # baseline's, and the journal's window additions carrying none
        # of the disturbance records a leaked attempt would leave.
        receipts1 = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
        if receipts1 != receipts0:
            failures.append(
                "the field owner's receipt log changed across the "
                "foreign peer's attempt"
            )
            raise Abort
        journal1 = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        if journal1[: len(journal0)] != journal0:
            failures.append(
                "the field owner's journal no longer answers its "
                "pre-attempt entries verbatim"
            )
            raise Abort
        added = journal1[len(journal0) :]
        leaked = [
            entry
            for entry in added
            if DISTURBANCE_EVENTS & set(entry.get("event", {}))
        ]
        if leaked:
            failures.append(
                "the field owner's journal gained disturbance records "
                f"during the foreign peer's attempt: {leaked}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "undisturbed",
                "receipts": receipts1,
                "journal_added": added,
            }
        )

        # Phase 6 — teardown: the foreign peer stops, and the original
        # pair must still converge — later legs see the same rig.
        stop(foreign)
        foreign = None
        tracked = pair.scan(standby_url, failures)
        owner = pair.scan(duty_url, failures)
        if pair.select_snapshot(tracked) != pair.select_snapshot(owner):
            failures.append(
                "the declared pair no longer converges after the "
                f"foreign peer's teardown — images diverged at tick "
                f"{owner['tick']}"
            )
            raise Abort
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        sync = standby_role.get("sync")
        if duty_role.get("role") != "active" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the declared pair did not return to active + "
                f"tracking after the teardown: {duty_role} / "
                f"{standby_role}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "teardown",
                "tick": owner["tick"],
                "duty_role": duty_role,
                "standby_role": standby_role,
            }
        )

        # Phase 7 — the control leg: the same third controller on the
        # pair's own model converges and promotes normally — the
        # refusal named the negotiation failure, not a rig defect.
        control, control_url, preamble = pair.spawn_peer(
            args.controller, args.model, args.dt, plant_addr, target, {}
        )
        if control_url is None:
            raise Abort(
                "the control peer exited at startup: "
                f"{'; '.join(preamble) or 'no diagnostic'}"
            )
        ticks = []
        for _ in range(CONTROL_TICKS):
            adopted = pair.scan(control_url, failures)
            tracked = pair.scan(standby_url, failures)
            owner = pair.scan(duty_url, failures)
            if pair.select_snapshot(tracked) != pair.select_snapshot(owner):
                failures.append(
                    "the declared pair's images diverged while the "
                    f"control peer tracked: tick {owner['tick']}"
                )
                raise Abort
            if pair.select_snapshot(adopted) != pair.select_snapshot(owner):
                failures.append(
                    "the control peer's image diverged from the field "
                    f"owner's at tick {owner['tick']} — it did not "
                    "converge on the pair's own model"
                )
                raise Abort
            ticks.append(owner["tick"])
        control_role = pair.get(f"{control_url}/role", "GET /role", failures)
        sync = control_role.get("sync")
        if control_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the control peer never reported tracking — GET /role "
                f"answers {control_role}"
            )
            raise Abort
        status, promote = pair.request(f"{control_url}/promote", {})
        if status != 200 or promote.get("role") != "promoting":
            failures.append(
                "POST /promote on the converged control peer answered "
                f"{status} {promote}, expected a promoting report"
            )
            raise Abort
        pair.scan(control_url, failures)
        settled = pair.get(f"{control_url}/role", "GET /role", failures)
        if settled.get("role") != "active":
            failures.append(
                f"the promoted control peer reports "
                f"{settled.get('role')!r}, expected active"
            )
            raise Abort
        evidence["control_active_at"] = settled["tick"]
        digest_entries.append(
            {
                "phase": "control",
                "ticks": ticks,
                "converged_role": control_role,
                "promote": promote,
                "settled_role": settled,
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        stop(control)
        stop(foreign)
        stop(standby)
        stop(duty)
        stop(plant)
        shutil.rmtree(scratch, ignore_errors=True)
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
        choices=["expect-tracking"],
        help="flip the observation window's assertion to require "
        "convergence — the pass must fail naming the degraded "
        "negotiation report it actually saw",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = negotiation_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"negotiation: {line}")
        return 1
    for failure in failures:
        eprint(f"negotiation: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"negotiation: the {args.tamper} case passed silently — "
                "the leg never noticed the wrong expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"negotiation-digest {digest} — degraded "
        f"({evidence['degraded_detail']}) for {WINDOW_TICKS} scans, "
        "promote refused not_converged, control peer settled active "
        f"at tick {evidence['control_active_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
