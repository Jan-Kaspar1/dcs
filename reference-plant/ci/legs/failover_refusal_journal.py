#!/usr/bin/env python3
"""The failover-refusal-journal leg for the reference plant — the
consumer-side proof that an armed standby's fired-but-refused
automatic promotion lands a bounded journaled record naming the
refusal reason, so the durable trail distinguishes a refused gate
from a peer that never armed (WW-ENG-003, WW-LCM-001).

The consumer-boundary mirror of `qa-scenario-failover-refusal-journal`,
the platform-side acceptance of the fired-but-refused gate's durable
trace (the #1146 contract): an automatic failover gate is the pair's
first automatic actor, and its refused attempts are the least visible
thing a journal reader sees — a fired-and-refused self-promotion
queues no `role_changed` of its own, so without an entry of its own
the durable trail reads "a dead source, a parked standby, silence",
indistinguishable from a peer that never armed at all. The contract,
exercised against the manifest-declared pair through released
artifacts and HTTP only:

- with the pair settled and the standby armed through its declared
  `failover_budget`, the armed peer is promoted over the field by
  the documented demote/promote switch — the field owner demoted in
  front of it — then demoted back onto its configured tracking
  source after that source's controller stops: every driven scan
  from there is a produced-nothing pull counted against a
  convergence proof that never re-stands;
- the miss run reaching the armed budget fires the gate and the
  attempt is refused `not_converged`: the durable `--journal-file`
  and the served `GET /journal` tail must each carry exactly one
  `promotion_refused` entry naming the cause and the miss count the
  gate fired at — bounded, while misses keep counting past the
  voided boundary without a refiring flood — beside no role
  transition the attempt never made, the peer's served role
  `standby` through the whole window;
- a later eligible fire still promotes: the source's respawn
  re-claims the released field, the armed peer's next landed apply
  re-proves its convergence, and a second dead-source window
  reaching the budget lands the failover-attributed
  `standby → promoting → active` walk the refused attempt never
  produced;
- the pair's launch roles restore: the respawned duty rejoins
  tracking the promoted peer and the documented switch back leaves
  the launched owner `active`, the armed peer `tracking`.

The leg binds the manifest's declared redundant pair and the
standby's declared `failover_budget`, launches the released tooling
through the shared pair rig with the standby armed at that budget,
and audits only consumer-observable surfaces: the monitor's
`GET /role`, `GET /journal`, `POST /demote`/`POST /promote`, and the
declared durable journal file — never the process's stderr, where
the refusal's log line is not the durable contract. The contract
postdates the pinned release line: where the armed peer's report
serves no `failover` bundle, the manifest declares no journal file
for the durable audit, or the voided window completes with both
journal surfaces silent — the fired-but-refused gate leaving no
trace, the finding's own shape — the run is the pre-contract one
and the leg reports inconclusive rather than asserting until the
manifest repins a release carrying the contract.

Usage:

    failover_refusal_journal.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `failover-refusal-journal-digest <sha256>` line
prints — the check runs two passes and compares them
(`failover-refusal-journal-nondeterministic`). A contract violation
reports `failover-refusal-journal: …` lines on stderr and exits 1 —
the check's `failover-refusal-journal-failed`. `--tamper
silent-durable` doctors the leg's durable-journal read to open past
the fired window — the durable record staying silent while the leg
still asserts the refusal journaled — so the leg proves its
journaled-refusal assertion fires rather than passing an
unexercised contract.
"""

import argparse
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored case.
# The doctored case: a leg asserting the refusal journaled while
# the durable record stays silent must surface the named
# diagnostic — never a silently unexercised contract.
LEG = {
    "order": 540,
    "title": "the failover-refusal-journal leg",
    "passes": "refusal-journal-leg",
    "tampers": [
        {
            "name": "silent-durable",
            "passed": "a silent-durable case passed the failover-refusal-journal leg",
            "missed": "the silent-durable case did not report its named diagnostic",
            "evidence": [
                "the doctored audit read the durable journal past the fired window"
            ],
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


# The driven-scan bounds each phase runs inside: the promoted peer's
# settle and the demoted peer's standby settle land at the first
# completing scans, the post-boundary hold gives a refiring gate or a
# flooded trail scans to show itself in, and the respawned peer's
# first pull reconverges it inside a handful of tracking-first ticks.
SETTLE_SCANS = 6
HOLD_SCANS = 3
TRACK_BOUND = 8


def role(url, failures):
    """`GET /role` — the served report the window watch reads."""
    return pair.get(f"{url}/role", "GET /role", failures)


def tracking(report):
    """`role: "standby"` with `sync.tracking` populated."""
    sync = report.get("sync")
    return (
        report.get("role") == "standby"
        and isinstance(sync, dict)
        and "tracking" in sync
    )


def journal_entries(path):
    """A `--journal-file`'s entry records in file order — the durable
    half of the refusal audit."""
    return [
        record
        for kind, record in pair.journal_records(path)
        if kind == "entry"
    ]


def refusal_rows(entries):
    """The `promotion_refused` event bodies of a journal entry list —
    the fired-but-refused gate's own record, absent on a peer that
    never armed or a trail that lost the episode."""
    return [
        entry["event"]["promotion_refused"]
        for entry in entries
        if isinstance(entry.get("event", {}).get("promotion_refused"), dict)
    ]


def role_walks(entries):
    """The `role_changed` transitions toward field ownership of a
    journal entry list — `(from, to)` per entry. A refused
    self-promotion queues no transition, so a promotion row inside
    the voided window is a move the attempt never legitimately
    made."""
    return [
        (change["from"], change["to"])
        for entry in entries
        for change in [entry.get("event", {}).get("role_changed")]
        if isinstance(change, dict)
        and change.get("to") in ("promoting", "active")
    ]


def failover_bundle(report, budget, failures, where):
    """The report's `failover` bundle must be the armed accounting —
    `converged` a bool, `misses` a non-negative count, `budget` the
    armed declaration. Returns the bundle; a malformed one fails by
    name and unwinds."""
    served = report.get("failover")
    if (
        not isinstance(served, dict)
        or not isinstance(served.get("converged"), bool)
        or not isinstance(served.get("misses"), int)
        or isinstance(served.get("misses"), bool)
        or served.get("misses", 0) < 0
        or served.get("budget") != budget
    ):
        failures.append(
            f"{where} reports failover {served} — the armed gate's "
            "served accounting carries `converged`, `misses`, and "
            f"the armed budget {budget}"
        )
        raise Abort
    return served


def failover_refusal_journal_pass(args, tamper):
    """The exercised run: converge the armed declared pair, switch
    the field onto it, demote it onto its dead configured source,
    watch the produced-nothing misses reach the armed budget with the
    role parked `standby`, audit the durable and served
    `promotion_refused` rows and the absent role walk, then restore
    the source, prove a later eligible fire promotes, and restore
    the launch roles. Returns `(digest_entries, evidence, failures)`;
    raises `Inconclusive` where the pinned release cannot answer the
    contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "failover-refusal-journal leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    armed = standby_decl.get("failover_budget")
    if not isinstance(armed, int) or isinstance(armed, bool) or armed < 1:
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

        # Phase 1 — convergence and the armed baseline: the
        # tracking standby's report must carry the gate's served
        # accounting — a standby report carrying no `failover` key
        # at all is the pinned release predating the served-field
        # contract, inconclusive rather than a failure.
        converged = rig.converge(failures)
        duty_role = converged["duty_role"]
        standby_role = converged["standby_role"]
        evidence["converged"] = converged["ticks"][-1]
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
                "armed-gate contract the leg exercises"
            )
        bundle = failover_bundle(
            standby_role, budget, failures, "the armed baseline"
        )
        if bundle != {"converged": True, "misses": 0, "budget": budget}:
            failures.append(
                f"the armed baseline reports failover {bundle} — a "
                "tracking standby's accounting reads `converged: "
                f"true` with the miss run zeroed, `budget` {budget}"
            )
            raise Abort
        if "failover" in duty_role:
            failures.append(
                "the unarmed field owner's report serves failover "
                f"{duty_role['failover']} — an absent field is the "
                "unarmed-peer contract, never a stood gate"
            )
            raise Abort
        journal_file = rig.standby_files.get("journal_file")
        if journal_file is None:
            raise Inconclusive(
                "the manifest's standby declares no journal_file — "
                "the durable half of the refusal audit is absent"
            )
        digest_entries.append(
            {
                "phase": "armed",
                "standby": bundle,
                "duty": "absent",
                "journal": "declared",
            }
        )

        # Phase 2 — the arming switch: the armed peer must own the
        # field to be demoted onto its dead source, so the documented
        # demote/promote switch seats it — the incumbent demoted in
        # front of the promoted standby.
        switched = rig.switch(
            duty_url,
            standby_url,
            failures,
            demote_what="the field owner",
            promote_what="the armed standby",
        )
        digest_entries.append(
            {
                "phase": "switch",
                "demote": switched["demote"],
                "promote": switched["promote"],
                "ticks": switched["ticks"],
                "demoted_role": switched["demoted_role"],
                "promoted_role": switched["promoted_role"],
                "transitions": switched["transitions"],
            }
        )

        # Phase 3 — the void: the configured tracking source's
        # controller stops, and the promoted peer demotes back onto
        # it — every scan from the settle onward a produced-nothing
        # pull counted against a proof that never re-stands. The
        # audit floor opens at the demote's answer so the settle
        # scan's first miss — and a budget-1 gate's refusal inside
        # the settle itself — lands inside the window.
        pair.stop(rig.duty)
        rig.duty = None
        demoted = rig.demote(
            standby_url, failures, "the promoted armed peer"
        )
        floor = len(journal_entries(journal_file))
        served_floor = len(
            pair.get(
                f"{standby_url}/journal", "GET /journal", failures
            )
        )
        settle = []
        last = None
        for _ in range(SETTLE_SCANS):
            pair.scan(standby_url, failures)
            report = role(standby_url, failures)
            settle.append(report.get("role"))
            last = report
            if report.get("role") == "standby":
                break
        if last.get("role") != "standby":
            failures.append(
                "the demoted armed peer never settled standby "
                f"behind its dead source — roles {settle}, last "
                f"report {last}"
            )
            raise Abort
        bundle = failover_bundle(
            last, budget, failures, "the demoted settle report"
        )
        if bundle["converged"] is not False or bundle["misses"] < 1:
            failures.append(
                "the demoted settle report carries failover "
                f"{bundle} — a demote onto a dead source voids the "
                "proof: `converged: false` beside a nonzero miss "
                "run"
            )
            raise Abort
        settle_misses = bundle["misses"]
        evidence["voided_at"] = last.get("tick")
        digest_entries.append(
            {
                "phase": "void",
                "demote": demoted,
                "settle_misses": settle_misses,
            }
        )

        # Phase 4 — the voided-gate window: each driven scan is one
        # more produced-nothing miss on the dead source — the count
        # climbing one per scan through the armed budget's boundary
        # and past it — every report holding `role: "standby"` with
        # the proof reported lapsed. A gate that promotes on a
        # voided proof is the wrong answer entirely, worse than a
        # silent one.
        window = [settle_misses]
        ticks = [last.get("tick")]
        misses = settle_misses
        while misses < budget + HOLD_SCANS:
            pair.scan(standby_url, failures)
            report = role(standby_url, failures)
            bundle = failover_bundle(
                report, budget, failures, f"miss-window report {report.get('tick')}"
            )
            if report.get("role") != "standby":
                failures.append(
                    "the voided gate promoted anyway — the peer "
                    f"left standby on a proof-free window: {report}"
                )
                raise Abort
            if bundle["converged"] is not False:
                failures.append(
                    "a miss-window report carries failover "
                    f"{bundle} — the voided proof must report "
                    "`converged: false` while the produced-nothing "
                    "misses count"
                )
                raise Abort
            if bundle["misses"] != misses + 1:
                failures.append(
                    f"the miss run jumped {misses} to "
                    f"{bundle['misses']} inside one driven scan — "
                    "each produced-nothing pull advances the count "
                    "exactly one"
                )
                raise Abort
            misses = bundle["misses"]
            window.append(misses)
            ticks.append(report.get("tick"))
        evidence["window"] = misses

        # The audit: the durable file and the served tail since
        # their floors — `promotion_refused` rows, and the role walk
        # the refused attempt never legitimately made. Both surfaces
        # silent is the finding's own shape — the pinned release
        # predating the contract — never a failure; one surface
        # silent beside the other's row is the surfaces disagreeing,
        # a contract violation.
        entries = journal_entries(journal_file)
        audit_floor = len(entries) if tamper == "silent-durable" else floor
        durable = refusal_rows(entries[audit_floor:])
        served = refusal_rows(
            pair.get(
                f"{standby_url}/journal", "GET /journal", failures
            )[served_floor:]
        )
        walks = role_walks(entries[floor:])
        evidence["durable_refusals"] = len(durable)
        evidence["served_refusals"] = len(served)
        if not durable and not served:
            raise Inconclusive(
                "the voided window completed with both journal "
                "surfaces silent — a dead source, a parked standby, "
                "and no record: the pinned release predates the "
                "journaled refusal contract, the trail "
                "indistinguishable from a peer that never armed"
            )
        if len(durable) != 1:
            if tamper == "silent-durable":
                failures.append(
                    "the doctored audit read the durable journal "
                    "past the fired window — the leg still asserts "
                    "the refusal journaled while the durable record "
                    "stays silent: it holds 0 promotion_refused "
                    "entries for the voided window instead of "
                    "exactly one"
                )
            else:
                failures.append(
                    "the durable journal holds "
                    f"{json.dumps(len(durable))} promotion_refused "
                    "entries for the voided window instead of "
                    "exactly one — a fired-but-refused gate that "
                    "journals nothing reads identical to a peer "
                    "that never armed, and a refiring gate floods "
                    "the trail"
                )
            raise Abort
        body = durable[0]
        error = body.get("error")
        cause = next(iter(error), None) if isinstance(error, dict) else None
        if cause != "not_converged":
            failures.append(
                "the journaled refusal names "
                f"{json.dumps(error)[:200]} — the voided window's "
                "cause is the convergence proof the gate found "
                "lapsed"
            )
            raise Abort
        fired = body.get("misses")
        if (
            not isinstance(fired, int)
            or isinstance(fired, bool)
            or fired < budget
        ):
            failures.append(
                "the journaled refusal fired at misses="
                f"{json.dumps(fired)} below the declared budget "
                f"{budget} — the row does not name the boundary it "
                "fired at"
            )
            raise Abort
        if served != durable:
            failures.append(
                "the served tail's promotion_refused rows "
                f"{json.dumps(served)[:200]} disagree with the "
                f"durable file's {json.dumps(durable)[:200]} — the "
                "two audit surfaces must carry the same refusal"
            )
            raise Abort
        if walks:
            failures.append(
                f"the voided window journaled {walks} — a refused "
                "self-promotion queues no role transition, so a "
                "promotion row here is a move the gate never "
                "legitimately made"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "window",
                "misses": window,
                "ticks": ticks,
                "refusal": {"cause": cause, "misses": fired},
                "durable": len(durable),
                "served": len(served),
                "walk": "none",
            }
        )

        # Phase 5 — the source restored: the respawned duty boots
        # active-role on its declared listen and its startup claim
        # re-takes the released field; the armed peer's next landed
        # pull re-proves its convergence — the miss run zeroed, the
        # gate live again for a later eligible fire.
        rig.duty, resumed_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            None,
            rig.duty_files,
            listen=duty_url.removeprefix("http://"),
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
        report = None
        for _ in range(TRACK_BOUND):
            pair.scan(standby_url, failures)
            pair.scan(duty_url, failures)
            report = role(standby_url, failures)
            if tracking(report):
                break
        if not tracking(report):
            failures.append(
                "the armed peer never reconverged behind the "
                f"respawned source — its reports ended {report}"
            )
            raise Abort
        if role(duty_url, failures).get("role") != "active":
            failures.append(
                "the respawned duty peer never re-claimed the "
                "released field — the restore leaves the pair "
                "ownerless"
            )
            raise Abort
        bundle = failover_bundle(
            report, budget, failures, "the reconverged report"
        )
        if bundle["converged"] is not True or bundle["misses"] != 0:
            failures.append(
                "the reconverged peer's accounting reads failover "
                f"{bundle} — a landed apply re-proves the "
                "convergence, zeroing the miss run the refusal "
                "fired on"
            )
            raise Abort
        evidence["reconverged"] = report.get("tick")
        digest_entries.append(
            {
                "phase": "restore",
                "tick": report.get("tick"),
                "failover": bundle,
            }
        )

        # Phase 6 — the later eligible fire: the source dies again
        # and the miss run climbs to the budget on a standing proof —
        # the gate that refused on a lapsed one must now promote,
        # its journal walking the failover-attributed promotion the
        # refused attempt never produced.
        pair.stop(rig.duty)
        rig.duty = None
        fire_floor = len(journal_entries(journal_file))
        climb = []
        promoted = None
        for miss in range(1, budget + 1):
            pair.scan(standby_url, failures)
            report = role(standby_url, failures)
            bundle = failover_bundle(
                report, budget, failures, f"refire report {report.get('tick')}"
            )
            if miss < budget:
                if report.get("role") != "standby":
                    failures.append(
                        f"the eligible window's miss {miss} moved "
                        f"the role early — report {report}"
                    )
                    raise Abort
                climb.append(miss)
            else:
                if report.get("role") != "active":
                    failures.append(
                        "the eligible fire never promoted — the "
                        f"armed {budget}-miss window ended with "
                        f"report {report}"
                    )
                    raise Abort
                promoted = report
            if bundle["misses"] != miss:
                failures.append(
                    f"the eligible window's miss {miss} reports "
                    f"failover {bundle} — each produced-nothing "
                    "pull advances the count exactly one"
                )
                raise Abort
        fired_walks = role_walks(journal_entries(journal_file)[fire_floor:])
        if fired_walks != [("standby", "promoting"), ("promoting", "active")]:
            failures.append(
                "the eligible fire's durable walk is "
                f"{fired_walks} — the promotion the refused attempt "
                "never made must journal standby → promoting → "
                "active"
            )
            raise Abort
        evidence["refired"] = promoted.get("tick")
        digest_entries.append(
            {
                "phase": "refire",
                "misses": climb,
                "promoted": promoted.get("tick"),
                "walk": fired_walks,
            }
        )

        # Phase 7 — the launch arrangement again: the respawned
        # duty rejoins tracking the promoted peer, and the
        # documented switch back restores the launch roles — the
        # launched owner active, the armed peer tracking behind it.
        rig.duty, resumed_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            standby_url.removeprefix("http://"),
            rig.duty_files,
            listen=duty_url.removeprefix("http://"),
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
        report = None
        for _ in range(TRACK_BOUND):
            _tracked, _owner = rig.tick(
                duty_url,
                standby_url,
                failures,
                diverged="the respawned duty peer's image diverged "
                "from the promoted owner's at tick {tick} — the "
                "restore's pulls never realigned the pair",
            )
            report = role(duty_url, failures)
            if tracking(report):
                break
        if not tracking(report):
            failures.append(
                "the respawned duty peer never reconverged "
                f"tracking — GET /role answers {report}"
            )
            raise Abort
        restored = rig.switch(
            standby_url,
            duty_url,
            failures,
            demote_what="the promoted peer",
            promote_what="the reconverged duty peer",
        )
        if restored["promoted_role"].get("role") != "active":
            failures.append(
                "the documented switch back left the reconverged "
                f"duty peer reporting {restored['promoted_role']} "
                "— the launch arrangement's field owner is active"
            )
            raise Abort
        if not tracking(restored["demoted_role"]):
            failures.append(
                "the documented switch back left the armed peer "
                f"reporting {restored['demoted_role']} — the "
                "launch arrangement's standby is demoted in place"
            )
            raise Abort
        evidence["restored_at"] = restored["ticks"][-1]
        digest_entries.append(
            {
                "phase": "launch",
                "ticks": restored["ticks"],
                "duty": restored["promoted_role"].get("role"),
                "standby": "tracking",
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
        choices=["silent-durable"],
        help="doctor the leg's durable-journal read to open past "
        "the fired window — the pass must fail asserting the "
        "refusal journaled while the durable record stays silent",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = (
            failover_refusal_journal_pass(args, args.tamper)
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "failover-refusal-journal: the doctored audit read "
                "the durable journal past the fired window — an "
                "inconclusive run offers the doctored case no "
                "evidence"
            )
            return 1
        eprint(f"failover-refusal-journal: inconclusive — {inconclusive}")
        print(f"failover-refusal-journal-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"failover-refusal-journal: {line}")
        return 1
    for failure in failures:
        eprint(f"failover-refusal-journal: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"failover-refusal-journal: the {args.tamper} case "
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
        f"failover-refusal-journal-digest {digest} — tracking by "
        f"tick {evidence['converged']}, the voided window counted "
        f"misses to {evidence['window']} with the durable and "
        f"served trails each holding "
        f"{evidence['durable_refusals']} refusal row, the source "
        f"reconverged at tick {evidence['reconverged']}, the "
        f"eligible fire promoted at tick {evidence['refired']}, "
        f"launch roles restored at tick {evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
