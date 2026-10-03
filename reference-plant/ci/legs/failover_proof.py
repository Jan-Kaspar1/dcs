#!/usr/bin/env python3
"""The failover-proof leg for the reference plant — the consumer-side
proof that the armed standby's served standing proof and heartbeat
miss accounting outlast a checkpoint-source outage and bound its
self-promotion to the declared budget's scan boundary (WW-ENG-003,
WW-LCM-001).

The consumer-boundary mirror of `qa-scenario-failover-proof-report`,
the platform-side acceptance of the served failover gate's evidence
(`role-report-failover-proof`, #1029) — exercised against the
manifest-declared pair through released artifacts and HTTP only. The
failover leg (`ci/legs/failover.py`) proves the unattended switch
itself; this leg proves the gate's evidence is *served* — the
`GET /role` report's `failover` bundle carrying `converged` (the
standing promotion proof the self-promotion gate reads), `misses`
(the consecutive checkpoint-pull count it is bounded by), and
`budget` (the armed declaration the count is read against) — so a
consumer watches the gate's math outrun an outage instead of watching
the role flip. The contract, from the consumer side:

- an armed tracking peer's report carries `failover` —
  `{converged, misses, budget}`; an unarmed peer's report carries no
  `failover` key, the standing proof served only beside the armed
  budget it is read against;
- a severed checkpoint source degrades the standby's sync while the
  standing proof reports each miss — `misses` *k* of `budget` *N* —
  on reports that keep `role: "standby"` through the window;
- the report at the boundary precedes self-promotion: the final
  in-window report answers `misses: N-1`, the budget-th miss's scan
  boundary settles the peer `active`, and the promoted report still
  serves the accounting the promotion consumed —
  `{converged: true, misses: N, budget: N}`;
- restoring the checkpoint source reconverges the pair: the
  respawned duty peer returns `tracking` off the promoted peer's
  checkpoints — its unarmed report serving no `failover` key — the
  documented `POST /demote`/`POST /promote` switch restores the
  launch roles, and the re-stood standby's proof reads a zeroed miss
  run again.

The leg binds the manifest's declared redundant pair and the standby's
declared `failover_budget`, launches the released tooling through the
shared pair rig with the standby armed at that budget, converges it
over `pair.tick` tracking-first driven ticks, then exercises the
degraded-window contract through `GET /role`:

1.  the converged reports record the armed baseline — the standby's
    `failover` `{converged: true, misses: 0, budget: N}` beside the
    field owner's absent field. A standby report carrying no
    `failover` key at all is the pinned release predating the
    served-field contract — inconclusive, never a failure;
2.  `pair.stop(rig.duty)` — the stop lever — severs the standby's
    checkpoint source. Each driven scan advances the miss run one
    count, and `GET /role` must keep answering `role: "standby"`
    under a `degraded` sync with `failover` reporting `misses` *k* of
    *N* for k = 1..N-1 — the standing proof and its miss accounting
    outlasting the outage, no in-window report moving the role early;
3.  the scan that takes the miss run to the declared budget is the
    promotion's own boundary: `GET /role` answers `role: "active"`
    at the expected tick with the consumed accounting `misses: N`
    still served — the last standby report (tick T-1, `misses: N-1`)
    immediately preceding the promoted report (tick T, `misses: N`)
    exactly as `failover_due` reads the gate;
4.  `pair.spawn_peer` restores the source: the respawned duty peer
    resumes on its declared listen address and recorded durable
    state, and driven tracking-first pair ticks reconverge it
    `tracking` off the promoted peer's checkpoints — its unarmed
    report serving no `failover` key;
5.  `rig.switch` performs the documented demote/promote handover,
    restoring the launch roles — the original duty `active` again,
    the original standby `tracking` with the standing proof re-stood
    at `misses: 0` of N.

Everything the leg asserts is consumer-observable: monitor HTTP and
the rig-owned persistence the respawn reopens. The leg never imports
or builds against the platform tree; `ci/check.sh`'s boundary lint
greps the leg sources for checkout paths, and the two digest-identical
passes the check runs replay the digested reports verbatim.

Tamper `stripped-proof` doctors the degraded-window expectation — the
window's reports are checked *absent* `failover`, as if the standing
proof and miss accounting were never served. Any honest run's served
field is reported against the doctored wording; a release too old to
serve the field gives the doctored case nothing to check and fails
the self-check rather than passing silently.

Usage:

    failover_proof.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `failover-proof-digest <sha256>` line prints — the
check runs two passes and compares them
(`failover-proof-nondeterministic`). A contract violation reports
`failover-proof: …` lines on stderr and exits 1 — the check's
`failover-proof-failed`. A converged standby report carrying no
`failover` field is the pinned release predating the contract —
inconclusive, not a failure. `--tamper stripped-proof` doctors the
leg's degraded-window expectation, so the leg proves its
standing-proof assertion fires rather than passing an unexercised
contract.
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored case.
# The doctored case: a leg checking the degraded window stripped of
# its served failover evidence must surface the named diagnostic on
# the honest served proof — never a silently unexercised contract.
LEG = {
    "order": 400,
    "title": "the failover-proof leg",
    "passes": "failover-proof-leg",
    "tampers": [
        {
            "name": "stripped-proof",
            "passed": "a stripped-proof case passed the failover-proof leg",
            "missed": "the stripped-proof case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the degraded window "
                "stripped of its failover evidence"
            ],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the served-field contract the leg
    exercises — the run classifies inconclusive, never a product
    failure."""


# The driven tick bound the respawned duty peer reconverges inside —
# the tracker's first pull applies the promoted owner's checkpoint, so
# the bound only covers startup margin.
TRACK_BOUND = 8


def tracking(report):
    """`role: "standby"` with `sync.tracking` populated."""
    sync = report.get("sync")
    return (
        report.get("role") == "standby"
        and isinstance(sync, dict)
        and "tracking" in sync
    )


def role(url, failures):
    """GET /role — the served report the whole leg reads."""
    return pair.get(f"{url}/role", "GET /role", failures)


def check_failover_evidence(report, budget, failures, where):
    """The report's `failover` bundle must be the armed standing proof
    — `converged` a bool, `misses` a non-negative count, `budget` the
    armed declaration, no keys beside. Returns the validated bundle; a
    malformed bundle fails by name and unwinds."""
    served = report.get("failover")
    if not isinstance(served, dict):
        failures.append(
            f"{where} reports failover {served!r} — the armed standing "
            "proof is served as an object carrying `converged`, "
            "`misses`, `budget`"
        )
        raise Abort
    extra = set(served) - {"converged", "misses", "budget"}
    missing = {"converged", "misses", "budget"} - set(served)
    if missing or extra:
        failures.append(
            f"{where} reports failover keys {sorted(served)} — the "
            "contract serves exactly `converged`, `misses`, `budget`"
        )
        raise Abort
    if (
        not isinstance(served["converged"], bool)
        or not isinstance(served["misses"], int)
        or isinstance(served["misses"], bool)
        or not isinstance(served["budget"], int)
        or isinstance(served["budget"], bool)
        or served["misses"] < 0
        or served["budget"] != budget
    ):
        failures.append(
            f"{where} reports failover {served} — `converged` a bool, "
            f"`misses` a non-negative count, `budget` the armed {budget}"
        )
        raise Abort
    return served


def failover_proof_pass(args, tamper):
    """The exercised run: converge the armed declared pair, sever the
    standby's checkpoint source, assert the served standing proof and
    miss accounting across the degraded window and at the promotion's
    boundary, restore the source, and restore the launch roles.
    Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release cannot answer the
    contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "failover-proof leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    armed = standby_decl.get("failover_budget")
    if (
        not isinstance(armed, int)
        or isinstance(armed, bool)
        or armed < 1
    ):
        raise Abort(
            "the manifest's standby declares no positive "
            "failover_budget — the deployed pair's armed heartbeat "
            "is absent"
        )
    budget = armed
    digest_entries, evidence, failures = [], {"budget": budget}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared, auto_promote=budget)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence and the armed baseline: driven
        # tracking-first ticks until the standby tracks the field
        # owner, then the reports the contract keys on. The converged
        # standby's report must carry the standing proof with a zeroed
        # miss run; the unarmed field owner's report carries no
        # `failover` key. No `failover` key at all is the pre-contract
        # release — inconclusive.
        converged = rig.converge(failures)
        duty_role = converged["duty_role"]
        standby_role = converged["standby_role"]
        severed_at = standby_role["tick"]
        evidence["converged"] = severed_at
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": duty_role,
                "standby_role": standby_role,
            }
        )
        baseline = standby_role.get("failover")
        if baseline is None:
            raise Inconclusive(
                "the converged standby's report serves no failover "
                "field — the pinned release predates the served "
                "standing-proof contract the leg exercises"
            )
        check_failover_evidence(
            standby_role, budget, failures, "the armed baseline"
        )
        want = {"converged": True, "misses": 0, "budget": budget}
        if baseline != want:
            failures.append(
                f"the armed baseline reports failover {baseline} — a "
                "tracking standby's standing proof reads `converged: "
                f"true` with the miss run zeroed, `budget` the armed "
                f"{budget}"
            )
            raise Abort
        if "failover" in duty_role:
            failures.append(
                "the unarmed field owner's report serves failover "
                f"{duty_role['failover']} — an absent field is the "
                "unarmed-peer contract, never a stood proof"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "armed",
                "standby": baseline,
                "duty": "absent",
            }
        )

        # Phase 2 — the degraded window: the stop lever severs the
        # standby's checkpoint source; driven scans advance the miss
        # run one count per scan, the served standing proof reporting
        # `misses` k of N on `role: "standby"` reports that keep a
        # degraded sync at the scan's own tick. The doctored negative
        # checks the window stripped — any served field is reported
        # against the doctored wording.
        pair.stop(rig.duty)
        rig.duty = None
        evidence["severed_at"] = severed_at
        misses = []
        for miss in range(1, budget):
            pair.scan(standby_url, failures)
            report = role(standby_url, failures)
            if tamper == "stripped-proof":
                served = report.get("failover")
                if served is not None:
                    failures.append(
                        f"miss {miss}'s degraded report carries the "
                        f"standing proof {served} — the doctored "
                        "expectation wanted the degraded window "
                        "stripped of its failover evidence"
                    )
                    raise Abort
            else:
                if report.get("role") != "standby":
                    failures.append(
                        f"miss {miss}'s report moves the role to "
                        f"{report.get('role')!r} inside the window — "
                        "the standing proof bounds miss accounting "
                        "before any self-promotion, the role holding "
                        "standby until the armed budget's boundary"
                    )
                    raise Abort
                sync = report.get("sync")
                if not (isinstance(sync, dict) and "degraded" in sync):
                    failures.append(
                        f"miss {miss}'s report carries sync {sync} — "
                        "a severed tracking pull degrades the "
                        "standby's sync variant while the role holds "
                        "standby"
                    )
                    raise Abort
                if report.get("tick") != severed_at + miss:
                    failures.append(
                        f"miss {miss}'s report stands at tick "
                        f"{report.get('tick')} — each driven scan "
                        f"advances the run one tick, expected "
                        f"{severed_at + miss} off the severance at "
                        f"{severed_at}"
                    )
                    raise Abort
                served = check_failover_evidence(
                    report, budget, failures, f"miss {miss}'s report"
                )
                want = {
                    "converged": True,
                    "misses": miss,
                    "budget": budget,
                }
                if served != want:
                    failures.append(
                        f"miss {miss}'s report serves failover "
                        f"{served} — the standing proof reports "
                        "`converged: true` with miss accounting "
                        f"{want['misses']} of {budget}"
                    )
                    raise Abort
            misses.append(
                {
                    "miss": miss,
                    "tick": report.get("tick"),
                    "failover": served,
                }
            )
        evidence["miss_run"] = [entry["miss"] for entry in misses]
        digest_entries.append(
            {
                "phase": "miss-run",
                "severed_at": severed_at,
                "misses": misses,
            }
        )

        # Phase 3 — the boundary: the scan that takes the miss run to
        # the declared budget is the promotion's own scan boundary —
        # the last in-window report (tick T-1, `misses: N-1`)
        # immediately preceding the promoted report (tick T, the
        # consumed `misses: N` still served) exactly as
        # `failover_due` reads the gate.
        pair.scan(standby_url, failures)
        promoted = role(standby_url, failures)
        if promoted.get("role") != "active":
            failures.append(
                f"the armed {budget}-miss window ended without "
                f"self-promotion — GET /role answers {promoted}"
            )
            raise Abort
        promotion_tick = promoted.get("tick")
        if promotion_tick != severed_at + budget:
            failures.append(
                f"self-promotion landed at tick {promotion_tick} — "
                f"the declared {budget}-miss window from tick "
                f"{severed_at} bounds the promotion at tick "
                f"{severed_at + budget}"
            )
            raise Abort
        if misses and misses[-1]["tick"] != promotion_tick - 1:
            failures.append(
                "the standing proof's last in-window report at tick "
                f"{misses[-1]['tick']} does not immediately precede "
                f"the promoted report at tick {promotion_tick} — the "
                "report at the boundary precedes self-promotion"
            )
            raise Abort
        if tamper is None:
            served = check_failover_evidence(
                promoted, budget, failures, "the promoted report"
            )
            want = {
                "converged": True,
                "misses": budget,
                "budget": budget,
            }
            if served != want:
                failures.append(
                    f"the promoted report serves failover {served} — "
                    "the promotion's consumed accounting reads "
                    f"{want}"
                )
                raise Abort
            boundary_failover = served
        else:
            boundary_failover = promoted.get("failover")
        evidence["promoted"] = promotion_tick
        digest_entries.append(
            {
                "phase": "boundary",
                "promoted": {
                    "tick": promotion_tick,
                    "role": promoted["role"],
                    "failover": boundary_failover,
                },
            }
        )

        # Phase 4 — restore: the source's respawn on its declared
        # listen address resumes the duty peer on its recorded
        # durable state; driven tracking-first pair ticks reconverge
        # it `tracking` off the promoted peer's checkpoints — its
        # unarmed report serving no `failover` key.
        duty_listen = duty_url.removeprefix("http://")
        rig.duty, resumed_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            standby_url.removeprefix("http://"),
            rig.duty_files,
            listen=duty_listen,
            pair_token=pair.PAIR_TOKEN,
        )
        rig.duty_url = resumed_url or rig.duty_url
        if resumed_url is None:
            failures.append(
                "the respawned duty controller "
                f"{duty_decl['name']} exited at startup: "
                f"{'; '.join(preamble) or 'no diagnostic'}"
            )
            raise Abort
        resumed = []
        duty_report = None
        for _ in range(TRACK_BOUND):
            _tracked, owner = rig.tick(
                duty_url,
                standby_url,
                failures,
                diverged="the respawned duty peer's image diverged "
                "from the promoted owner's at tick {tick} — the "
                "restore's pulls never realigned the pair",
            )
            resumed.append(owner["tick"])
            report = role(duty_url, failures)
            if tracking(report):
                duty_report = report
                break
        if duty_report is None:
            failures.append(
                "the respawned duty peer never reconverged tracking "
                f"inside the declared {TRACK_BOUND}-tick window — "
                f"GET /role answers {report}"
            )
            raise Abort
        if "failover" in duty_report:
            failures.append(
                "the unarmed respawned peer's report serves failover "
                f"{duty_report['failover']} — an absent field is the "
                "unarmed-peer contract, never a stood proof"
            )
            raise Abort
        evidence["reconverged"] = resumed[-1]
        digest_entries.append(
            {
                "phase": "restore",
                "resumed": resumed,
                "duty": {
                    "role": duty_report["role"],
                    "tick": duty_report["tick"],
                    "sync": duty_report["sync"],
                    "failover": "absent",
                },
            }
        )

        # Phase 5 — the launch arrangement again: the documented
        # demote/promote switch restores the launch roles — the
        # reconverged duty peer active, the originally armed standby
        # tracking with the standing proof re-stood at a zeroed miss
        # run beside the unarmed owner's absent field.
        restored = rig.switch(
            standby_url,
            duty_url,
            failures,
            demote_what="the promoted peer",
            promote_what="the reconverged duty peer",
            audit_receipts=True,
        )
        if restored["promoted_role"].get("role") != "active":
            failures.append(
                "the documented switch back left the reconverged "
                f"duty peer reporting {restored['promoted_role']} — "
                "the launch arrangement's field owner is active"
            )
            raise Abort
        if "failover" in restored["promoted_role"]:
            failures.append(
                "the unarmed restored owner's report serves failover "
                f"{restored['promoted_role']['failover']} — an absent "
                "field is the unarmed-peer contract, never a stood "
                "proof"
            )
            raise Abort
        if not tracking(restored["demoted_role"]):
            failures.append(
                "the documented switch back left the promoted peer "
                f"reporting {restored['demoted_role']} — the launch "
                "arrangement's standby is demoted in place"
            )
            raise Abort
        if tamper is None:
            re_stood = restored["demoted_role"].get("failover")
            want = {"converged": True, "misses": 0, "budget": budget}
            if re_stood != want:
                failures.append(
                    "the re-stood standby's report serves failover "
                    f"{re_stood} — a tracking armed peer's standing "
                    f"proof reads {want} after the launch roles "
                    "restore"
                )
                raise Abort
        evidence["restored_at"] = restored["ticks"][-1]
        digest_entries.append(
            {
                "phase": "failback",
                "demote": restored["demote"],
                "promote": restored["promote"],
                "ticks": restored["ticks"],
                "demoted_role": restored["demoted_role"],
                "promoted_role": restored["promoted_role"],
                "receipts": restored["receipts"],
                "transitions": restored["transitions"],
            }
        )
        return digest_entries, evidence, failures
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
        choices=["stripped-proof"],
        help="doctor the leg's degraded-window expectation — the pass "
        "must fail naming the served standing proof",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = failover_proof_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "failover-proof: the doctored expectation wanted the "
                "degraded window stripped of its failover evidence — "
                "an inconclusive run offers the doctored case no "
                "evidence"
            )
            return 1
        eprint(f"failover-proof: inconclusive — {inconclusive}")
        print(f"failover-proof-inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"failover-proof: {line}")
        return 1
    for failure in failures:
        eprint(f"failover-proof: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"failover-proof: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"failover-proof-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the standing proof reported "
        f"misses {evidence['miss_run']} of {evidence['budget']} "
        "through the degraded window, self-promoted at tick "
        f"{evidence['promoted']} on the reported boundary, the "
        "respawned duty reconverged tracking by tick "
        f"{evidence['reconverged']}, launch roles restored at tick "
        f"{evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
