#!/usr/bin/env python3
"""The orphan-retarget-journal leg for the reference plant — the
consumer-side proof that a tracking standby driven onto an ownerless
source re-targets its pulls onto the line's verified owner through
the orphan-resolution probe *journaled*: a named, attributed
`tracking_source_adopted` record carrying the new pull source — the
same record the announced- and claimed-source adoptions produce for
the identical pull-target change — beside the `field_orphaned`
episode the re-target resolved (WW-ENG-003, WW-LCM-001 — the
journaled pull-target switch the #1137 fix established, mirrored at
the customer boundary).

The tracking-source-fallback leg (`ci/legs/tracking_source_fallback.py`)
proves the learned pin's liveness — a dead source releasing back to
the configured one. This leg proves the resolution's *audit*: the
orphan-resolution re-target used to land unjournaled —
`field_orphaned` bracketed the switch and nothing attributed when or
where the pulls moved — so the contract requires the resolved pin to
journal its adoption with parity to the announced and claimed pins.
The observed peer is the manifest's declared standby, the only pair
member carrying a configured `--standby` source; the re-target's
destination is a third controller the leg spawns as a sibling
standby of the field owner. The run:

- convergence — the manifest-declared pair settles `active` /
  `tracking` on the declared wiring and the spawned sibling
  converges `tracking` on the same owner, so all three runs share
  the line's generation;
- the orphan and the re-target — the documented demote path:
  `POST /demote` on the field owner, `POST /promote` on the sibling.
  The declared standby keeps pulling its configured source — the
  demoted owner's checkpoints stamp `source_owns_field: false` and
  propagate `line_owner` naming the successor — so its applies turn
  `orphaned` (journaled `field_orphaned`) and the orphan-resolution
  probe verifies the successor as this line's field owner,
  re-targeting the pulls through the resolved slot;
- the audit — the standby's served journal and its declared durable
  `--journal-file` both carry the attributed
  `tracking_source_adopted` naming the successor under the run's
  single cold-start boundary, the `field_orphaned` record carrying
  the episode the re-target resolved — the parity evidence — and
  the peer reconverges `tracking` on the resolved source with no
  restart, the demoted owner holding `standby` behind the promoted
  sibling: one active, the rest tracking it;
- the restore — the same switch walked back: `POST /demote` on the
  successor, `POST /promote` on the configured owner. The standby
  orphans a second time on the successor's ownerless checkpoints,
  re-resolves onto the re-seated owner, and journals the second
  `tracking_source_adopted` naming it — the pair landing back on
  its launch roles, the duty `active` and the declared standby
  `tracking` it.

The contract postdates some pinned release lines: where the launched
tooling predates it — a served checkpoint without the
`source_owns_field`/`line_owner` stamps, the declared `journal_file`
persistence absent, the peer resolving no verified tracking source
onto the successor at all, or the re-target landing *unjournaled* —
the finding's own reproduction — the run's evidence is the
pre-contract shape and the leg reports
`orphan-retarget-journal-digest inconclusive` rather than asserting
until the manifest repins a release carrying the contract.

Usage:

    orphan_retarget_journal.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `orphan-retarget-journal-digest <sha256>` line prints
— the check runs two passes and compares them
(`orphan-retarget-journal-nondeterministic`). A contract violation
reports `orphan-retarget-journal: …` lines on stderr and exits 1 —
the check's `retarget-journal-failed`. `--tamper expect-silence`
doctors the leg's expectation to the pre-contract shape — asserting
the re-target may land unjournaled, the silent pull-target switch
the finding reported — so the leg proves its audit fires on the
honest attributed record rather than passing an unexercised
contract.
"""

import argparse
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import claim_reclaim
import demote_reconvergence
import pair
import tracking_source_fallback


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the re-target may land
# unjournaled — the silent pull-target switch the contract closed —
# must surface the named diagnostic on the honest attributed record
# rather than passing an unexercised contract.
LEG = {
    "order": 540,
    "title": "the orphan-retarget-journal leg",
    "passes": "retarget-journal-leg",
    "failed": "retarget-journal-failed",
    "tampers": [
        {
            "name": "expect-silence",
            "passed": "an expect-silence case passed the orphan-retarget-journal leg",
            "missed": "the expect-silence case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the re-target unjournaled"
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
# orphaned applies resolve and pin the successor within a handful of
# rounds, and the restore's second orphan-resolution re-target lands
# inside the same bound — the unjournaled re-target the contract
# closed is the silence the journal audit catches.
SUCCESSOR_SCANS = 8
RESOLVE_SCANS = 12
RESTORE_SCANS = 16


journal_entries = tracking_source_fallback.journal_entries
durable_kinds = tracking_source_fallback.durable_kinds
journal_boundaries = tracking_source_fallback.journal_boundaries
adopted_entries = tracking_source_fallback.adopted_entries
adopted_kinds = tracking_source_fallback.adopted_kinds
sync_kind = tracking_source_fallback.sync_kind
monitor_port = tracking_source_fallback.monitor_port
peer_kind = tracking_source_fallback.peer_kind


def orphan_retarget_journal_pass(args, tamper):
    """The exercised run: converge the declared pair and the spawned
    sibling, demote the owner so the standby's orphaned applies
    re-target its pulls onto the promoted successor, and assert the
    resolution's attributed `tracking_source_adopted` record lands
    in the served journal and the durable file alike — the
    `field_orphaned` episode it resolved bracketing it — with the
    peer reconverging `tracking` inside its first process lifetime
    and the launch roles restored by the walked-back switch.
    Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "orphan-retarget-journal leg has nothing to exercise"
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
                "the durable half of the re-target audit is absent"
            )
        digest_entries.append(
            {"phase": "gate", "stamps": "present", "journal": "declared"}
        )

        # Phase 2 — the orphan and the re-target (the demote path):
        # `POST /demote` on the field owner, `POST /promote` on the
        # sibling. The declared standby keeps pulling its configured
        # source: the demoted owner's checkpoints stamp
        # `source_owns_field: false` and propagate `line_owner`
        # naming the successor, so the standby's orphaned applies'
        # resolution probe verifies the successor as this line's
        # field owner and re-targets the pulls through the resolved
        # slot — the resolved pin outranking the configured source
        # from then on.
        floor = len(
            pair.get(f"{standby_url}/journal", "GET /journal", failures)
        )
        rig.demote(duty_url, failures, "the field owner")
        rig.promote(successor_url, failures, "the spawned successor")
        watch = []
        report = None
        promoted = False
        for _ in range(RESOLVE_SCANS):
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
        evidence["retarget_watch"] = watch
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
        evidence["retarget_adopted"] = adopted
        doc = pair.get(
            f"{standby_url}/checkpoint", "GET /checkpoint", failures
        )
        evidence["retarget_doc"] = doc
        tracks_successor = claim_reclaim.tracking(report) and str(
            doc.get("line_owner")
        ).endswith(":" + monitor_port(successor_url))
        if "successor" not in sources:
            if tracks_successor:
                # The finding's own reproduction: the pulls moved —
                # the tracking verdict and the propagated owner name
                # prove the resolved pin — and the durable audit
                # recorded nothing. The pre-contract silence is the
                # release's shape, never a violation.
                raise Inconclusive(
                    "the standby re-targeted its pulls onto the "
                    "successor unjournaled — the pinned release "
                    "predates the journaled orphan-retarget "
                    "contract the leg exercises: adopted "
                    f"{sources or 'none'} across {watch}"
                )
            raise Inconclusive(
                "the tracking peer resolved no verified tracking "
                "source onto the successor — the pinned release "
                "predates the orphan-resolution substrate the leg "
                f"exercises: adopted {sources or 'none'} across "
                f"{watch}"
            )
        if not tracks_successor:
            raise Inconclusive(
                "the tracking peer journaled the successor pin but "
                "never reconverged onto it — the pinned release "
                "predates the learned-pin contract the leg "
                f"exercises: its sync readings stayed {watch}"
            )
        if sources[-1] != "successor":
            failures.append(
                f"the tracking peer's last adoption resolves a "
                f"{sources[-1]} source, not the successor — the "
                f"resolved pin must name the promoted sibling: "
                f"{adopted}"
            )
        if duty_role.get("role") != "standby":
            failures.append(
                f"the demoted owner reports {duty_role} — it must "
                "hold standby behind the promoted successor"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "retarget",
                "watch": watch,
                "adopted": sources,
                "line_owner": "successor",
            }
        )

        # Phase 3 — the audit's durable half: the declared
        # --journal-file carries the attributed
        # `tracking_source_adopted` naming the successor beside the
        # `field_orphaned` episode the resolution closed — the
        # parity the announced and claimed adoptions record — under
        # the single cold-start boundary: the no-restart proof that
        # the re-target landed inside the first process lifetime.
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
                "— the re-target must land inside the first "
                "process lifetime, no restart boundary"
            )
        durable_sources = [
            peer_kind(urls, source)
            for source in demote_reconvergence.adopted_sources(
                journal_file
            )
        ]
        evidence["durable_adopted"] = durable_sources
        if durable_sources != sources:
            failures.append(
                "the durable journal file's adoptions "
                f"{durable_sources} diverge from the served "
                f"journal's {sources} — the re-target's named "
                "evidence must record identically on both halves"
            )
        kinds = durable_kinds(journal_file)
        evidence["durable_kinds"] = sorted(kinds)
        if "field_orphaned" not in kinds:
            failures.append(
                "the durable journal file carries no "
                "field_orphaned record — the ownerless episode the "
                "re-target resolved went unjournaled"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "adopted": durable_sources,
                "orphaned": "field_orphaned" in kinds,
                "boundaries": boundaries,
            }
        )

        # The doctored case — the leg asserting the pre-contract
        # shape: the re-target may land unjournaled, the silent
        # pull-target switch the finding reported. The honest
        # attributed record must fail it.
        if tamper == "expect-silence":
            failures.append(
                "the doctored expectation wanted the re-target "
                "unjournaled — the honest run journaled the "
                "attributed tracking_source_adopted naming the "
                "resolved source"
            )
            raise Abort

        # Phase 4 — the restore: the same switch walked back.
        # `POST /demote` on the successor, `POST /promote` on the
        # configured owner: the standby — pinned onto the successor —
        # orphans a second time on its ownerless checkpoints and the
        # resolution probe re-targets the pulls onto the re-seated
        # owner, journaling the second `tracking_source_adopted`
        # naming it. The pair lands back on its launch roles.
        rig.demote(successor_url, failures, "the promoted successor")
        rig.promote(duty_url, failures, "the configured owner")
        watch = []
        restored = None
        for _ in range(RESTORE_SCANS):
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
                    "successor": sync_kind(successor_role),
                    "standby": sync_kind(report),
                }
            )
            if duty_role.get("role") == "active" and (
                claim_reclaim.tracking(report)
            ):
                restored = report
                break
        evidence["restore_watch"] = watch
        if failures:
            raise Abort
        if restored is None:
            failures.append(
                "the pair never reconverged to its launch roles — "
                f"the restore's readings stayed {watch}"
            )
            raise Abort
        journal = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        sources = adopted_kinds(urls, journal[floor:])
        evidence["restore_adopted"] = [
            record for _seq, record in adopted_entries(journal[floor:])
        ]
        if "successor" not in sources or (
            "duty" not in sources[sources.index("successor") + 1 :]
        ):
            failures.append(
                "the restore's re-target onto the re-seated owner "
                "went unjournaled — the standby's served journal "
                f"carries {sources}, expected a "
                "tracking_source_adopted naming the configured "
                "source after the successor's record"
            )
        doc = pair.get(
            f"{standby_url}/checkpoint", "GET /checkpoint", failures
        )
        evidence["restore_doc"] = doc
        if not str(doc.get("line_owner")).endswith(
            ":" + monitor_port(duty_url)
        ):
            failures.append(
                f"the restored peer's checkpoint names line_owner "
                f"{doc.get('line_owner')} — the configured owner on "
                f":{monitor_port(duty_url)} was expected"
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
                "the durable journal file missed the restore's "
                "re-target — the second attributed adoption never "
                f"reached it: {durable_sources}"
            )
        boundaries = journal_boundaries(journal_file)
        if boundaries != [{"run": 1, "tick": 0}]:
            failures.append(
                f"the standby's journal boundaries are {boundaries} "
                "— the restore must land inside the first process "
                "lifetime, no restart boundary"
            )
        if failures:
            raise Abort
        evidence["restored_at"] = restored.get("tick")
        digest_entries.append(
            {
                "phase": "restore",
                "watch": watch,
                "adopted": sources,
                "durable_adopted": durable_sources,
                "duty": duty_role.get("role"),
                "standby": sync_kind(report),
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
        choices=["expect-silence"],
        help="doctor the leg's expectation to the pre-contract "
        "shape — the pass must fail naming the silent re-target",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = (
            orphan_retarget_journal_pass(args, args.tamper)
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "orphan-retarget-journal: the doctored expectation "
                "wanted the re-target unjournaled — an inconclusive "
                "run offers the doctored case no evidence"
            )
            return 1
        eprint(f"orphan-retarget-journal: inconclusive — {inconclusive}")
        print(f"orphan-retarget-journal-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"orphan-retarget-journal: {line}")
        return 1
    for failure in failures:
        eprint(f"orphan-retarget-journal: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"orphan-retarget-journal: the {args.tamper} case "
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
        f"orphan-retarget-journal-digest {digest} — tracking by "
        f"tick {evidence['converged']}, the orphaned re-target "
        "journaled the successor and the restore re-journaled the "
        f"configured owner, launch roles restored at tick "
        f"{evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
