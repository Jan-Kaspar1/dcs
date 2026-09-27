"""failover-proof — the redundant pair's served standing proof, from the
field edge.

Issue #1035, the consumer-boundary exercise of the failover gate's
standing proof and miss accounting (issue #1029 serves it, QA carries
`qa-scenario-failover-proof-report` as the platform-facing scenario):
the standby's `GET /role` report must expose the evidence
`Peer::self_promote` reads — `converged` the standing promotion proof,
`misses` the consecutive checkpoint-pull count it bounds — so a
consumer can watch the gate's math outrun an outage instead of
watching the role flip. The contract, from the consumer side:

* An armed tracking peer's report carries `failover` —
  `{converged, misses, budget}`; an unarmed peer's report carries no
  `failover` key.
* With the pair converged under the armed budget, a severed
  checkpoint source degrades the standby's sync while the standing
  proof reports each miss — `misses` k of `budget` N — on reports
  that keep `role: "standby"` through the window.
* The report at the boundary precedes self-promotion: the final
  in-window report answers `misses: N-1`, the budget-th miss's scan
  boundary settles the peer `active`, and the promoted report still
  serves the accounting the promotion consumed —
  `{converged: true, misses: N, budget: N}`.
* Restoring the checkpoint source reconverges the pair: the
  respawned duty peer returns `tracking` off the promoted peer's
  checkpoints (its unarmed report serving no `failover` key), the
  documented `POST /demote`/`POST /promote` switch restores the
  launch roles, and the re-stood standby's proof reads a zeroed miss
  run again.

The leg binds the manifest's declared redundant pair and declared
`failover_budget`, launches the released monitor binary in `--driven`
scan mode under the leg's `--run-dir` evidence area, converges the
standby over `pair.tick` handover ticks, then exercises the
degraded-window contract through the released `GET /role` contract:

1.  `role(contract_url, failures)` on the converged standby records
    the armed baseline — `failover` `{converged: true, misses: 0,
    budget: N}`; the field owner's report carries no `failover` key
    (an unarmed peer serves none). A release whose standby report has
    no `failover` key at all predates the served-field contract and
    is inconclusive.
2.  `pair.stop(rig.duty)` — the stop lever — severs the standby's
    checkpoint source. Driven scans advance the miss run; after each,
    `GET /role` must answer `role: "standby"` with a degraded sync
    and `failover: {converged: true, misses: k, budget: N}` for k =
    1..N-1 — the standing proof and miss accounting outlasting the
    outage, no report inside the window moving the role early.
3.  The scan that takes the miss run to the declared budget is the
    promotion's own boundary: `GET /role` afterward answers
    `role: "active"` at the declared tick with the consumed
    accounting `misses: N` still served — the last standby report
    (tick T-1, `misses: N-1`) preceding the promoted report
    (tick T, `misses: N`) exactly as `failover_due` reads the gate.
4.  `pair.spawn_peer` restores the source: the respawned duty peer
    resumes on its recorded durable state and driven tracking-first
    pair ticks reconverge it `tracking` off the promoted peer's
    checkpoints. `rig.switch` performs the documented demote/promote
    handover, restoring the launch roles — the original duty `active`
    again, the original standby `tracking` with the standing proof
    re-stood at `misses: 0` of N.

Everything the leg asserts is consumer-observable: monitor HTTP and
the rig-owned evidence it launches under `--run-dir`. The leg never
imports or builds against the platform tree; `ci/check.sh`'s boundary
lint greps the leg sources for checkout paths, and the rig's
`digest_entries` are what the pair-run test replays verbatim twice.

Tamper `stripped-proof` doctors the degraded-window expectations —
the window's reports are checked *absent* `failover`, as if the
standing proof and miss accounting were never served. Any honest
run's served field is reported against the doctored wording; a
release too old to serve the field gives the doctored case nothing
to check and fails the self-check rather than passing silently.

Since the field itself is the contract, the leg is *inconclusive*
when the pinned release predates it — the converged standby's report
carrying no `failover` key — rather than asserting the served bundle
a release predating #1029 cannot answer.

Exit 1 with the leg's named failure (`failover-proof-failed` in the
release contract) when any contract assertion fails; inconclusive —
with exit 0 and a `failover-proof-digest inconclusive` line — names
what the run could not observe. Two clean passes must emit the same
digest.
"""

import argparse
import hashlib
import json
import shutil
import sys

import pair


class Abort(Exception):
    pass


class Inconclusive(Exception):
    pass


LEG = {
    "order": 400,
    "title": "redundant pair — served failover standing proof and miss accounting",
    "passes": "failover-proof",
    "tampers": [
        {
            "case": "stripped-proof",
            "evidence": [
                "the doctored expectation wanted the degraded window "
                "stripped of its failover evidence"
            ],
        }
    ],
}


CONVERGE_TICKS = 4
TRACK_BOUND = 8


def tracking(report):
    """`role: "standby"` with `sync.tracking` populated."""
    return (
        report.get("role") == "standby"
        and isinstance(report.get("sync"), dict)
        and "tracking" in report["sync"]
    )


def eprint(message):
    print(f"failover-proof: {message}", file=sys.stderr)


def role(url, failures):
    """GET /role — the served report the whole leg reads."""
    return pair.get(f"{url}/role", "GET /role", failures)


def arm_common():
    ap = argparse.ArgumentParser(prog="failover-proof")
    ap.add_argument("--controller", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--plant", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dt", type=float, default=0.05)
    ap.add_argument("--tamper", choices=["stripped-proof"], default=None)
    ap.add_argument(
        "--run-dir",
        help="launch the pair under this existing directory instead of "
        "a private temp directory",
    )
    return ap


def check_failover_evidence(report, budget, failures, where):
    """The report's `failover` bundle must be the armed standing proof
    — `converged`, `misses`, `budget` all integers/bool the contract
    serves. Returns the validated bundle; a present-but-malformed or
    mismatched bundle fails by name."""
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


def failover_proof_pass(args, tamper=None):
    """The exercised phase, shared by the real run and the doctored
    negative. Returns `(digest_entries, evidence, failures)`; raises
    `Abort` for setup failures and `Inconclusive` when the pinned
    release cannot exercise the contract."""
    failures = []
    manifest = pair.load_manifest(args.manifest)
    declared = manifest.get("failover_budget")
    if declared is None:
        raise Abort(
            "the scenario manifest declares no failover_budget — the "
            "pair-leg contract carries the armed budget"
        )
    if (
        not isinstance(declared, int)
        or isinstance(declared, bool)
        or declared < 1
    ):
        raise Abort(
            f"the scenario manifest declares failover_budget "
            f"{declared!r} — the budget is a positive miss count"
        )
    budget = declared
    pair_decl = manifest["pair"]
    launch_dir = args.run_dir
    if launch_dir is None:
        launch_dir = pair.fresh_dir(args.out, "failover-proof")
    evidence = {"budget": budget}
    rig = pair.launch_pair(
        args.controller,
        args.model,
        args.dt,
        pair_decl,
        failures,
        auto_promote=budget,
        run_dir=launch_dir,
    )
    try:
        # Phase 1 — converge: driven tracking-first ticks until the
        # standby tracks the field owner.
        rig.converge(CONVERGE_TICKS)
        duty_url, standby_url = rig.urls
        duty_decl, standby_decl = rig.decls
        duty_role = role(duty_url, failures)
        standby_role = role(standby_url, failures)
        if not tracking(standby_role):
            failures.append(
                "the standby did not report tracking at convergence — "
                f"GET /role answers {standby_role}"
            )
            raise Abort
        evidence["converged"] = rig.tick_count
        digest_entries = [
            {
                "phase": "converge",
                "ticks": rig.tracked_ticks,
                "duty_role": duty_role,
                "standby_role": standby_role,
            }
        ]

        # Phase 2 — the armed baseline: the converged standby's report
        # carries the standing proof with a zeroed miss run; the
        # unarmed field owner's report carries no `failover` key. No
        # `failover` key at all is the pre-contract release —
        # inconclusive.
        baseline = standby_role.get("failover")
        if baseline is None:
            raise Inconclusive(
                "the converged standby's report serves no failover "
                "field — the pinned release predates the served "
                "standing-proof contract the leg exercises"
            )
        baseline = check_failover_evidence(
            standby_role, budget, failures, "the armed baseline"
        )
        if baseline != {"converged": True, "misses": 0, "budget": budget}:
            failures.append(
                "the armed baseline reports failover "
                f"{baseline} — a tracking standby's standing proof "
                f"reads `converged: true` with the miss run zeroed, "
                f"`budget` the armed {budget}"
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

        # Phase 3 — the degraded window: the stop lever severs the
        # standby's checkpoint source; driven scans advance the miss
        # run one per scan, the served standing proof reporting
        # `misses` k of N on `role: "standby"` reports that keep a
        # degraded sync. The doctored negative checks the window
        # stripped — any served field is reported against the
        # doctored wording.
        pair.stop(rig.duty)
        rig.duty = None
        severed_at = rig.tick_count
        evidence["severed_at"] = severed_at
        misses = []
        for miss in range(1, budget):
            rig.scan(standby_url)
            report = role(standby_url, failures)
            served = report.get("failover")
            if tamper == "stripped-proof":
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
                if (
                    not isinstance(report.get("sync"), dict)
                    or "degraded" not in report["sync"]
                ):
                    failures.append(
                        f"miss {miss}'s report carries sync "
                        f"{report.get('sync')} — a severed tracking "
                        "pull degrades the standby's sync variant "
                        "while the role holds standby"
                    )
                    raise Abort
                served = check_failover_evidence(
                    report,
                    budget,
                    failures,
                    f"miss {miss}'s report",
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
                        f"`converged: true` with miss accounting "
                        f"{want['misses']} of {budget}"
                    )
                    raise Abort
            misses.append(
                {
                    "miss": miss,
                    "tick": report["tick"],
                    "failover": served,
                }
            )
        evidence["miss_run"] = [m["miss"] for m in misses]
        digest_entries.append(
            {
                "phase": "miss-run",
                "severed_at": severed_at,
                "misses": misses,
            }
        )

        # Phase 4 — the boundary: the scan that takes the miss run to
        # the declared budget is the promotion's own scan boundary —
        # the last in-window report (tick T-1, `misses: N-1`)
        # preceding the promoted report (tick T, `misses: N` still
        # served) exactly as the gate reads it.
        promoted_tick = rig.scan(standby_url)
        promoted = role(standby_url, failures)
        if promoted.get("role") != "active":
            failures.append(
                f"the armed {budget}-miss window ended without "
                f"self-promotion — GET /role answers {promoted} at "
                f"tick {promoted_tick}"
            )
            raise Abort
        if promoted_tick != severed_at + budget:
            failures.append(
                f"self-promotion landed at tick {promoted_tick} — "
                f"the declared {budget}-miss window from tick "
                f"{severed_at} bounds the promotion at tick "
                f"{severed_at + budget}"
            )
            raise Abort
        if misses and misses[-1]["tick"] != promoted_tick - 1:
            failures.append(
                "the standing proof's last in-window report at tick "
                f"{misses[-1]['tick']} does not immediately precede "
                f"the promoted report at tick {promoted_tick} — the "
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
        evidence["promoted"] = promoted_tick
        digest_entries.append(
            {
                "phase": "boundary",
                "promoted": {
                    "tick": promoted_tick,
                    "role": promoted["role"],
                    "failover": boundary_failover,
                },
            }
        )

        # Phase 5 — restore: the source's respawn resumes the duty
        # peer on its recorded durable state; driven tracking-first
        # pair ticks reconverge it `tracking` off the promoted peer's
        # checkpoints — its unarmed report serving no `failover` key.
        standby_addr = standby_url.removeprefix("http://")
        duty_addr = duty_url.removeprefix("http://")
        rig.duty, resumed_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            standby_addr,
            rig.duty_files,
            listen=duty_addr,
            pair_token=pair.PAIR_TOKEN,
        )
        if resumed_url is None:
            raise Abort(
                "the respawned duty controller "
                f"{duty_decl['name']} exited at startup: "
                f"{'; '.join(preamble) or 'no diagnostic'}"
            )
        resumed = []
        duty_report = None
        for _ in range(TRACK_BOUND):
            _tracked, owner = rig.tick(duty_url, standby_url, failures)
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

        # Phase 6 — the launch arrangement again: the documented
        # demote/promote switch restores the launch roles — the
        # original duty active, the original standby tracking with
        # the standing proof re-stood at a zeroed miss run.
        restored = rig.switch(standby_url, duty_url, failures)
        if restored["promoted_role"].get("role") != "active":
            failures.append(
                "the documented switch back left the respawned duty "
                f"peer reporting {restored['promoted_role']} — the "
                "launch arrangement's field owner is active"
            )
            raise Abort
        if restored["demoted_role"].get("role") != "standby":
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
                "transitions": restored["transitions"],
            }
        )
        return digest_entries, evidence, failures
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        rig.close()
    return digest_entries, evidence, failures


def main():
    ap = arm_common()
    args = ap.parse_args()

    try:
        digest_entries, evidence, failures = failover_proof_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "the doctored expectation wanted the degraded window "
                "stripped of its failover evidence — an inconclusive "
                "run offers the doctored case no evidence"
            )
            return 1
        eprint(f"inconclusive — {inconclusive}")
        print(f"failover-proof-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(line)
        return 1
    for failure in failures:
        eprint(failure)
    if args.tamper is not None:
        if not failures:
            eprint(
                f"the {args.tamper} case passed silently — the leg "
                "never noticed the doctored expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"failover-proof-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the standing proof reported "
        f"misses {evidence['miss_run']} of {evidence['budget']} "
        f"through the degraded window, self-promoted at tick "
        f"{evidence['promoted']} on the reported boundary, "
        f"reconverged tracking by tick {evidence['reconverged']}, "
        f"launch roles restored at tick {evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
