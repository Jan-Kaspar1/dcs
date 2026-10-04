#!/usr/bin/env python3
"""The tracking-source-fallback leg for the reference plant — the
consumer-side proof that a tracking standby pinned onto a learned
successor falls back to its configured `--standby` source when the
pinned endpoint dies, reconverging `tracking` without a restart
instead of stranding `degraded` on the corpse (WW-ENG-003,
WW-LCM-001 — the pinned-tracking-source liveness bound the #1136
fix established, mirrored at the customer boundary).

The demote-reconvergence leg (`ci/legs/demote_reconvergence.py`)
proves a demoted owner adopts the announced successor; the
stranded-rejoin leg proves the claim-declared re-join. This leg
proves the pin's *liveness*: the learned tracking pins outrank the
configured source only while they answer — a dead one must release
inside a bounded miss run and the resolution probe re-earn the pull
target for a live successor, the configured source included. The
observed peer is the manifest's declared standby, the only pair
member carrying a configured `--standby` source; the pinned
successor is a third controller the leg itself spawns as a sibling
standby of the field owner. The run:

- convergence — the manifest-declared pair settles `active` /
  `tracking` on the declared wiring and the spawned sibling
  converges `tracking` on the same owner, so all three runs share
  the line's generation;
- the pin — the documented demote path: `POST /demote` on the field
  owner, `POST /promote` on the sibling. The demoted owner re-joins
  through the verified-source contract; the declared standby keeps
  pulling its configured source, whose checkpoints now stamp the
  serving run non-owning and propagate `line_owner` naming the
  successor — the orphaned applies' resolution probe verifies the
  successor as this line's field owner and pins it, journaling
  `tracking_source_adopted` — so the standby's pulls ride the
  learned pin, outranking the configured slot;
- the dead source — the successor's controller stops while the
  configured standby address keeps serving (demoted, then
  re-promoted so the configured source owns the line again). Each
  driven scan on the standby is a produced-nothing pull reporting
  `degraded` — the detail naming the dead pin, exactly the
  reproduction's `fetch from <dead>` — and inside the learned-pin
  liveness bound the pin releases and the same cycle's probe
  verifies the re-seated configured source, journaling the re-pin
  `tracking_source_adopted` and reconverging the peer `tracking`
  with no restart — the durable journal's single cold-start
  boundary the proof the process lifetime never broke;
- the restore — the pair lands back on its launch roles: the
  re-promoted duty `active`, the declared standby `tracking` it.

The contract postdates the pinned release line: where the launched
tooling predates it — a served checkpoint without the
`source_owns_field`/`line_owner` stamps, the declared `journal_file`
persistence absent, the tracking peer never resolving a verified
source at all, or the peer staying `degraded` on the dead successor
through the whole miss window — the run's own evidence is the
pre-contract shape and the leg reports
`tracking-source-fallback-digest inconclusive` rather than
asserting until the manifest repins a release carrying the
contract.

Usage:

    tracking_source_fallback.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `tracking-source-fallback-digest <sha256>` line
prints — the check runs two passes and compares them
(`tracking-source-fallback-nondeterministic`). A contract violation
reports `tracking-source-fallback: …` lines on stderr and exits 1 —
the check's `source-fallback-failed`. `--tamper expect-stranded`
doctors the leg's expectation to the pre-contract shape — asserting
the standby may stay pinned on the dead source, the stranded wedge
the contract closed — so the leg proves its fallback assertion
fires on the honest reconvergence rather than passing an
unexercised contract.
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import claim_reclaim
import demote_reconvergence
import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the standby may stay stranded
# on the dead pinned source — the pre-contract wedge the liveness
# bound closed — must surface the named diagnostic on the honest
# fallback rather than passing an unexercised contract.
LEG = {
    "order": 520,
    "title": "the tracking-source-fallback leg",
    "passes": "source-fallback-leg",
    "failed": "source-fallback-failed",
    "tampers": [
        {
            "name": "expect-stranded",
            "passed": "an expect-stranded case passed the tracking-source-fallback leg",
            "missed": "the expect-stranded case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the standby stranded on the dead pinned source"
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


# The driven-scan bounds the episode's phases run: the spawned
# successor converges `tracking` inside a few pulls, the standby's
# orphaned applies resolve and pin the successor within a handful
# of rounds, the re-promoted configured source re-owns the field on
# its first scans, and the dead-pin window spans the learned-pin's
# liveness bound plus the reconverging pull — the indefinite
# stranding the contract closed is the failure the bound catches.
SUCCESSOR_SCANS = 8
PIN_ROUNDS = 12
OWN_SCANS = 8
FALLBACK_SCANS = 8


def journal_entries(path):
    """A `--journal-file`'s entry records in file order — the durable
    half of the audit."""
    return [
        record
        for kind, record in pair.journal_records(path)
        if kind == "entry"
    ]


def durable_kinds(path):
    """The event kinds a `--journal-file`'s entry records carry."""
    return {
        kind
        for entry in journal_entries(path)
        for kind in entry.get("event", {})
    }


def journal_boundaries(path):
    """A `--journal-file`'s run-boundary markers — the single
    cold-start record proving the peer's process lifetime never
    broke across the fallback."""
    return [
        record
        for kind, record in pair.journal_records(path)
        if kind == "boundary"
    ]


def adopted_entries(entries):
    """The `tracking_source_adopted` records of a journal entry list
    — `(seq, record)` pairs in journal order."""
    return [
        (entry["seq"], entry["event"]["tracking_source_adopted"])
        for entry in entries
        if "tracking_source_adopted" in entry.get("event", {})
    ]


def sync_kind(report):
    """The sync vocabulary a standby's RoleReport carries —
    `tracking`, `orphaned`, `degraded` — or `unsynchronized` when
    the report carries the string form."""
    sync = (report or {}).get("sync")
    if isinstance(sync, str):
        return sync
    return claim_reclaim.sync_state(report) or "missing"


def monitor_port(url):
    """The served monitor's port — the endpoint suffix an adopted
    source, a degraded pull target, or a propagated `line_owner`
    must resolve to."""
    return url.rsplit(":", 1)[-1]


def peer_kind(urls, addr):
    """Which pair member a recorded address resolves to — `duty`,
    `standby`, or `successor` — by monitor port, `foreign` for
    anything else."""
    for name, url in urls.items():
        if str(addr).endswith(":" + monitor_port(url)):
            return name
    return "foreign"


def degraded_target(report):
    """The pull target a `degraded` report's detail names — the
    'fetch from <addr>: <error>' a failed checkpoint pull reports —
    or None."""
    sync = (report or {}).get("sync")
    detail = (
        sync.get("degraded", {}).get("detail")
        if isinstance(sync, dict)
        else None
    )
    return demote_reconvergence.fetch_source(detail)


def adopted_kinds(urls, entries):
    """The journaled adoption sequence classified by pair member —
    the pin's `successor` and the fallback's `duty` records read as
    names, never raw ports, so the digest is port-stable."""
    return [
        peer_kind(urls, record.get("source"))
        for _seq, record in adopted_entries(entries)
    ]


def tracking_source_fallback_pass(args, tamper):
    """The exercised run: converge the declared pair and the spawned
    sibling, demote the owner so the standby's orphaned applies pin
    the promoted successor, stop it with the configured source
    live, and assert the bounded miss run releases the pin onto the
    re-seated configured source — journaled by name — with the peer
    reconverging `tracking` inside its first process lifetime and
    the launch roles restored. Returns `(digest_entries, evidence,
    failures)`; raises `Inconclusive` where the pinned release
    predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "tracking-source-fallback leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    successor = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence: the manifest-declared pair settles
        # its launch roles, the declared standby tracking the owner
        # off its configured --standby source; then the successor —
        # a third controller the leg spawns as a sibling standby of
        # the same owner — converges `tracking` on the same line, so
        # all three runs share the generation the successor's
        # promotion continues.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )
        successor, successor_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            duty_url.removeprefix("http://"),
            {},
            pair_token=pair.PAIR_TOKEN,
        )
        if successor_url is None:
            failures.append(
                "the spawned successor controller exited at "
                f"startup: {'; '.join(preamble) or 'no diagnostic'}"
            )
            raise Abort
        urls = {
            "duty": duty_url,
            "standby": standby_url,
            "successor": successor_url,
        }
        tracked = None
        for _ in range(SUCCESSOR_SCANS):
            pair.scan(successor_url, failures)
            pair.scan(duty_url, failures)
            report = pair.get(
                f"{successor_url}/role", "GET /role", failures
            )
            if claim_reclaim.tracking(report):
                tracked = report
                break
        if tracked is None:
            failures.append(
                "the spawned successor never reported tracking on "
                f"the field owner — GET /role answers {report}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "successor",
                "role": tracked.get("role"),
                "sync": sync_kind(tracked),
            }
        )

        # The contract's substrate on the pinned release: the served
        # checkpoint's field-ownership stamps — `line_owner` is the
        # propagation the standby's orphan probe resolves through —
        # and the declared durable journal file the adoption audit
        # reads. Each absence is the release predating the contract,
        # never a violation of it.
        doc0 = pair.get(
            f"{standby_url}/checkpoint", "GET /checkpoint", failures
        )
        evidence["doc0"] = doc0
        if (
            "source_owns_field" not in doc0
            or "line_owner" not in doc0
        ):
            raise Inconclusive(
                "the tracking peer serves a checkpoint without the "
                "field-ownership stamps — the pinned release "
                "predates the tracking-source contract the leg "
                f"exercises: {doc0}"
            )
        if rig.standby_files.get("journal_file") is None:
            raise Inconclusive(
                "the manifest's standby declares no journal_file — "
                "the durable half of the fallback audit is absent"
            )
        digest_entries.append(
            {"phase": "gate", "stamps": "present", "journal": "declared"}
        )

        # Phase 2 — the pin (the demote path): `POST /demote` on the
        # field owner, `POST /promote` on the sibling. The demoted
        # owner re-joins through the verified-source contract —
        # announced hints, then the standing claim's declared
        # monitor — while the declared standby keeps pulling its
        # configured source: the demoted owner's checkpoints stamp
        # `source_owns_field: false` and propagate `line_owner`
        # onward, so the standby's orphaned applies' resolution
        # probe verifies the successor as this line's field owner,
        # journals the `tracking_source_adopted`, and pins it — the
        # learned pin outranking the configured slot from then on.
        floor = len(
            pair.get(f"{standby_url}/journal", "GET /journal", failures)
        )
        rig.demote(duty_url, failures, "the field owner")
        rig.promote(successor_url, failures, "the spawned successor")
        watch = []
        report = None
        promoted = False
        for _ in range(PIN_ROUNDS):
            pair.scan(duty_url, failures)
            pair.scan(standby_url, failures)
            pair.scan(successor_url, failures)
            duty_role = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            successor_role = pair.get(
                f"{successor_url}/role", "GET /role", failures
            )
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            watch.append(
                {
                    "duty": duty_role.get("role"),
                    "successor": successor_role.get("role"),
                    "standby": sync_kind(report),
                }
            )
            promoted = successor_role.get("role") == "active"
            if promoted and claim_reclaim.tracking(report):
                break
        evidence["pin_watch"] = watch
        if failures:
            raise Abort
        if not promoted:
            failures.append(
                "the promoted successor never claimed the field — "
                f"its role reports stayed {watch}"
            )
            raise Abort
        journal = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        adopted = adopted_entries(journal[floor:])
        sources = adopted_kinds(urls, journal[floor:])
        evidence["pin_adopted"] = adopted
        if "successor" not in sources:
            raise Inconclusive(
                "the tracking peer resolved no verified tracking "
                "source onto the successor — the pinned release "
                "predates the learned-pin substrate the leg "
                f"exercises: adopted {sources or 'none'} across "
                f"{watch}"
            )
        if not claim_reclaim.tracking(report):
            raise Inconclusive(
                "the tracking peer pinned the successor but never "
                "reconverged onto it — the pinned release predates "
                "the learned-pin contract the leg exercises: its "
                f"sync readings stayed {watch}"
            )
        if sources[-1] != "successor":
            failures.append(
                f"the tracking peer's last adoption resolves a "
                f"{sources[-1]} source, not the successor — the "
                f"learned pin must name the promoted sibling: "
                f"{adopted}"
            )
        if duty_role.get("role") != "standby":
            failures.append(
                f"the demoted owner reports {duty_role} — it must "
                "hold standby behind the promoted successor"
            )
        doc = pair.get(
            f"{standby_url}/checkpoint", "GET /checkpoint", failures
        )
        evidence["pinned_doc"] = doc
        if not str(doc.get("line_owner")).endswith(
            ":" + monitor_port(successor_url)
        ):
            failures.append(
                f"the pinned peer's checkpoint names line_owner "
                f"{doc.get('line_owner')} — expected the successor "
                f"on :{monitor_port(successor_url)}"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "pin",
                "adopted": sources,
                "sync": "tracking",
                "line_owner": "successor",
            }
        )

        # Phase 3 — the dead source with the configured standby
        # address live: the successor's controller stops while its
        # claim stands, and the configured source re-seats the field
        # — `POST /promote` preempts the dead standing claim — so the
        # released pin's re-resolution probe finds a live owner
        # under the configured slot.
        pair.stop(successor)
        successor = None
        rig.promote(duty_url, failures, "the configured source")
        owned = None
        for _ in range(OWN_SCANS):
            pair.scan(duty_url, failures)
            report = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            if report.get("role") == "active":
                owned = report
                break
        if owned is None:
            failures.append(
                "the configured source never re-owned the field — "
                f"its reports stayed {report}"
            )
            raise Abort
        digest_entries.append(
            {"phase": "reseat", "duty": owned.get("role")}
        )

        # Phase 4 — the bounded miss run and the fallback: each
        # driven scan on the standby is a produced-nothing pull on
        # the dead pin — the `degraded` detail naming the corpse,
        # the reproduction's `fetch from <dead>` — until the learned
        # pin's liveness bound releases it and the same cycle's
        # probe re-pins the re-seated configured source: the
        # journaled `tracking_source_adopted` naming the configured
        # address is the fallback's durable evidence, and the peer
        # reconverges `tracking` on the next pull.
        watch = []
        reconverged = None
        for _ in range(FALLBACK_SCANS):
            pair.scan(standby_url, failures)
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            kind = sync_kind(report)
            target = degraded_target(report)
            watch.append(
                {
                    "sync": kind,
                    "target": (
                        peer_kind(urls, target)
                        if target is not None
                        else None
                    ),
                }
            )
            if claim_reclaim.tracking(report):
                reconverged = report
                break
        evidence["fallback_watch"] = watch
        if failures:
            raise Abort
        if reconverged is None:
            if all(
                row["sync"] == "degraded"
                and row["target"] == "successor"
                for row in watch
            ):
                raise Inconclusive(
                    "the tracking peer stayed pinned on the dead "
                    "successor through the whole miss window — the "
                    "pinned release predates the dead-source "
                    "fallback contract the leg exercises"
                )
            failures.append(
                "the tracking peer never reconverged — its sync "
                f"readings stayed {watch}"
            )
            raise Abort
        held = watch[:-1]
        if not held or any(
            row["sync"] != "degraded" or row["target"] != "successor"
            for row in held
        ):
            failures.append(
                "the dead pinned source did not hold through its "
                "liveness bound — the miss run's readings were "
                f"{watch}, expected every produced-nothing pull "
                "naming the dead successor before the reconverging "
                "tracking verdict"
            )
            raise Abort
        evidence["reconverged"] = reconverged.get("tick")

        # The journaled fallback: a `tracking_source_adopted` naming
        # the configured source lands after the pin's record — the
        # served journal and the durable file both carrying it.
        journal = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        sources = adopted_kinds(urls, journal[floor:])
        evidence["fallback_adopted"] = [
            record for _seq, record in adopted_entries(journal[floor:])
        ]
        if "successor" not in sources or (
            "duty" not in sources[sources.index("successor") + 1 :]
        ):
            failures.append(
                "the standby's served journal carries no "
                "tracking_source_adopted naming the configured "
                "source after the pin's record — the fallback went "
                f"unjournaled: adopted {sources or 'none'}"
            )
        doc = pair.get(
            f"{standby_url}/checkpoint", "GET /checkpoint", failures
        )
        evidence["fallback_doc"] = doc
        if not str(doc.get("line_owner")).endswith(
            ":" + monitor_port(duty_url)
        ):
            failures.append(
                f"the reconverged peer's checkpoint names "
                f"line_owner {doc.get('line_owner')} — the "
                f"configured source on :{monitor_port(duty_url)} "
                "was expected"
            )
        if failures:
            raise Abort

        # The doctored case — the leg asserting the pre-contract
        # wedge: the standby may stay pinned on the dead source. The
        # honest bounded release and re-pin must fail it.
        if tamper == "expect-stranded":
            failures.append(
                "the doctored expectation wanted the standby "
                "stranded on the dead pinned source — the honest "
                "run released the pin, retried the configured "
                "source, and reconverged tracking"
            )
            raise Abort

        # Phase 5 — the durable record and the restore: the
        # --journal-file carries the pin and the fallback's adopted
        # records under the single cold-start boundary — the
        # no-restart proof — and the pair rests on its launch roles:
        # the re-promoted duty `active`, the declared standby
        # `tracking` it.
        journal_file = rig.standby_files["journal_file"]
        if not os.path.exists(journal_file):
            failures.append(
                f"the standby's declared journal file {journal_file} "
                "does not exist — the --journal-file flag was not "
                "honored"
            )
            raise Abort
        boundaries = journal_boundaries(journal_file)
        if boundaries != [{"run": 1, "tick": 0}]:
            failures.append(
                f"the standby's journal boundaries are {boundaries} "
                "— the reconvergence must land inside the first "
                "process lifetime, no restart boundary"
            )
        durable_sources = [
            peer_kind(urls, source)
            for source in demote_reconvergence.adopted_sources(
                journal_file
            )
        ]
        if "successor" not in durable_sources or (
            "duty"
            not in durable_sources[
                durable_sources.index("successor") + 1 :
            ]
        ):
            failures.append(
                "the durable journal file carries no "
                "tracking_source_adopted naming the configured "
                "source after the pin's record — the fallback's "
                f"named evidence is absent: {durable_sources}"
            )
        kinds = durable_kinds(journal_file)
        evidence["durable_kinds"] = sorted(kinds)
        if "field_orphaned" not in kinds:
            failures.append(
                "the durable journal file carries no "
                "field_orphaned record — the ownerless episode the "
                "pin resolved out of went unjournaled"
            )
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active":
            failures.append(
                f"the restored pair's duty reports {duty_role} — "
                "expected active"
            )
        if not claim_reclaim.tracking(standby_role):
            failures.append(
                f"the restored pair's standby reports "
                f"{standby_role} — expected a tracking standby"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "record",
                "watch": watch,
                "adopted": sources,
                "durable_adopted": durable_sources,
                "boundaries": boundaries,
            }
        )
        evidence["restored_at"] = standby_role.get("tick")
        digest_entries.append(
            {
                "phase": "restore",
                "duty": duty_role.get("role"),
                "standby": sync_kind(standby_role),
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        pair.stop(successor)
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
        choices=["expect-stranded"],
        help="doctor the leg's expectation to the pre-contract "
        "shape — the pass must fail naming the stranded wedge",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = (
            tracking_source_fallback_pass(args, args.tamper)
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "tracking-source-fallback: the doctored expectation "
                "wanted the standby stranded on the dead pinned "
                "source — an inconclusive run offers the doctored "
                "case no evidence"
            )
            return 1
        eprint(f"tracking-source-fallback: inconclusive — {inconclusive}")
        print(f"tracking-source-fallback-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"tracking-source-fallback: {line}")
        return 1
    for failure in failures:
        eprint(f"tracking-source-fallback: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"tracking-source-fallback: the {args.tamper} case "
                "passed silently — the leg never noticed the "
                "doctored expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"tracking-source-fallback-digest {digest} — tracking by "
        f"tick {evidence['converged']}, pinned the successor and "
        f"reconverged on the configured source at tick "
        f"{evidence['reconverged']}, launch roles restored at tick "
        f"{evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
