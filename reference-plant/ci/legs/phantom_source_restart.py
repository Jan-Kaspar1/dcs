#!/usr/bin/env python3
"""The phantom-source-restart leg for the reference plant — the
consumer-side mirror of the qa rig's phantom-source-restart leg
(qa_lane/scenarios/2195_phantom_source_restart.py), proving on the
released images that an uninterrupted same-generation checkpoint
stream never journals `source_restarted` while a genuine cold
restart of the tracked source still journals exactly one (WW-ENG-003,
WW-LCM-001 — the continuity contract the #694 fix established, pinned
in-workspace and on the rig but never on the customer-owned pair).

The defect the contract closed: a tracking peer's regression detector
treated any checkpoint below its last alignment — or below its own run
tick where no alignment stood — as a source restart, so a demoted
peer's first tracking pull, whose checkpoint trails the demoted run's
own tick because the demotion cleared the alignment, and a
same-generation peer merely lagging one scan both tripped it and
filled the durable journal with restarts that never happened (the
finding's `was_aligned: null` phantom on the demoted peer's own first
pull, and the one-tick regression beside it). The fix reads the
checkpoint's `generation` stamp: an uninterrupted successor still
stamps the generation the demoted run's own captures carried, so the
reset was the peer's tracking state and nothing journals, while a
cold-restarted source mints a new generation and the boundary is
preserved.

The run:

- converges the declared pair to its launch roles — the field owner
  `active`, the tracking standby `tracking` — through the pair rig's
  driven-tick loop, and gates the contract's surface: each peer's
  served checkpoint must carry the `generation` stamp the suppression
  reads and each declared durable `--journal-file` must exist. A
  pinned release predating that stamp reports
  `phantom-source-restart-digest inconclusive`, never a failure;
- runs the demote/promote cycle: the field owner demotes, the
  tracking standby promotes, and the demoted peer's own scans then
  pull its successor repeatedly while the successor leads by a scan
  each cycle — the first-pull lag and the mid-tracking one-tick
  regressions the clause names;
- audits the demoted peer through its serving monitor and its
  declared durable journal: no `source_restarted` may appear on that
  uninterrupted same-generation stream, and the served stream
  position it publishes — `stream_tick`, or its run tick where it
  declares no lead — must never regress while the tracking run
  realigns its local tick underneath the line;
- fails the pair back to its launch roles and repeats the cycle with
  the roles exchanged, so each peer's own demotion clears its own
  alignment and each is the audited tracker once;
- cold-restarts the field owner — its process stopped, its declared
  state file dropped, a fresh process respawned on its declared
  listen address with no `--state-file` to resume, so a new process
  mints a new generation and serves a stream regressed below the
  tracking peer's alignment. The tracking peer must then journal
  exactly one `source_restarted`, in its served journal and in its
  declared durable file alike, carrying the named evidence: the prior
  alignment as `was_aligned` and the resumed stream tick below it as
  `resumed_at`;
- asserts the pair rests on its launch roles again — the respawned
  owner's startup claim reclaims the field and the standby reconverges
  `tracking` onto it — for the legs behind this one.

Usage:

    phantom_source_restart.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `phantom-source-restart-digest <sha256>` line prints —
the check runs two passes and compares them
(`phantom-source-restart-nondeterministic`). A contract violation
reports `phantom-source-restart: …` lines on stderr and exits 1 — the
check's `source-restart-evidence-failed`. `--tamper planted-phantom`
plants a `source_restarted` entry into the demoted peer's
same-generation audit — the phantom the contract forbids — and
`--tamper missing-restart` withholds the cold restart while the leg
still asserts its one entry, so the audit reports the genuine restart
it never saw. Each must fail carrying the named evidence
(`phantom-source-restart-unchecked`).
"""

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import claim_reclaim
import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored cases. The
# doctored cases: a phantom restart planted into the demoted peer's
# same-generation audit — the defect's own lie — and a genuine
# restart the leg asserts while the cold restart never staged, each
# must surface the named diagnostic on the honest run rather than
# passing an unexercised audit.
LEG = {
    "order": 605,
    "title": "the phantom-source-restart leg",
    "passes": "phantom-source-restart",
    "failed": "source-restart-evidence-failed",
    "tampers": [
        {
            "name": "planted-phantom",
            "passed": "a planted-phantom case passed the phantom-source-restart leg",
            "missed": "the planted-phantom case did not report its named diagnostic",
            "evidence": ["a phantom restart the demote-to-track reset manufactures"],
        },
        {
            "name": "missing-restart",
            "passed": "a missing-restart case passed the phantom-source-restart leg",
            "missed": "the missing-restart case did not report its named diagnostic",
            "evidence": ["a genuinely cold-restarted source journals exactly one"],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates — or the manifest declares no
    surface for — the contract the leg exercises: the checkpoint's
    `generation` stamp, or a declared durable journal file to audit.
    The run classifies inconclusive, never a product failure."""


# The driven-scan bounds each phase runs: the demoted peer's repeated
# tracking applies — each a pull whose checkpoint trails the served
# stream by a scan — the cycle's settle into the reconciled posture,
# the reconvergence walk after the cold restart, and the launch-role
# hold. FETCH_SETTLE_S is the beat each requested checkpoint fetch
# gets to land inside before the next driven scan consumes it: the
# driven pair's puller answers one fetch per poll, so the fetch the
# scan before last requested is the one this scan applies.
CYCLE_SCANS = 6
SETTLE_SCANS = 4
RESTART_SCANS = 10
RESTORE_SCANS = 4
FETCH_SETTLE_S = 0.3


def int_tick(value):
    """Whether a report or journal field is an integer tick (not a
    bool, not absent)."""
    return isinstance(value, int) and not isinstance(value, bool)


def tracking(report):
    """Whether a role report shows the tracking standby posture the
    demoted peer must reconverge to."""
    return (
        isinstance(report, dict)
        and report.get("role") == "standby"
        and "tracking" in (report.get("sync") or {})
    )


def aligned_tick(report):
    """The stream tick a tracking report's alignment names, or None
    while the peer is unsynchronized or mid-transition."""
    aligned = ((report.get("sync") or {}).get("tracking") or {}).get(
        "aligned"
    )
    return aligned if int_tick(aligned) else None


def stream_position(checkpoint):
    """The position a served checkpoint declares in the tracked line's
    own tick domain: its `stream_tick` where the serving run carries a
    lead over that domain, its own run tick where it declares none —
    the only position a lead-free run can honestly claim."""
    if not isinstance(checkpoint, dict) or not int_tick(
        checkpoint.get("tick")
    ):
        return None
    declared = checkpoint.get("stream_tick")
    if int_tick(declared):
        return declared
    return checkpoint["tick"]


def restarts(entries):
    """The `source_restarted` entries a journal carries — each as
    `(seq, tick, was_aligned, resumed_at)` — for a served
    `GET /journal` entry list and a durable `--journal-file`'s entry
    records alike."""
    found = []
    for entry in entries:
        restart = (entry.get("event") or {}).get("source_restarted")
        if not isinstance(restart, dict):
            continue
        found.append(
            (
                entry.get("seq"),
                entry.get("tick"),
                restart.get("was_aligned"),
                restart.get("resumed_at"),
            )
        )
    return found


def served_restarts(url, failures):
    """The `source_restarted` entries a monitor's served journal
    carries."""
    return restarts(pair.get(f"{url}/journal", "GET /journal", failures))


def durable_restarts(path):
    """The `source_restarted` entries a declared `--journal-file`
    carries — the durable half of the audit."""
    return restarts(
        [
            record
            for kind, record in pair.journal_records(path)
            if kind == "entry"
        ]
    )


def contract_checkpoint(checkpoint):
    """Whether a served checkpoint carries the tick-domain contract
    surface the leg reads: the integer run `tick` and the `generation`
    the suppression is decided by. Absent, the pinned release predates
    the contract."""
    return (
        isinstance(checkpoint, dict)
        and int_tick(checkpoint.get("tick"))
        and int_tick(checkpoint.get("generation"))
    )


def clean_error(error, rig):
    """A failure detail with the rig's scratch path scrubbed, so two
    identical passes compare."""
    return str(error).replace(rig.scratch, "<run>")[:300]


def cycle(ctx):
    """One demote/promote cycle's observations: the demoted peer's
    served role rows, the served stream positions it and its successor
    published across the window, and the `source_restarted` census
    both sides report. `ctx` carries the rig, the failure sink, the
    promoted peer and the demoted peer's urls, its durable journal
    path, the floor its census starts past, and the tamper in force."""
    rig, failures = ctx["rig"], ctx["failures"]
    promoted, demoted = ctx["promoted"], ctx["demoted"]
    rows, served = [], []
    for _ in range(ctx["scans"]):
        # The demoted peer scans first so its pull applies the
        # promoted peer's latest checkpoint, then the promoted peer
        # advances its own served stream — the one-scan lead the
        # demoted peer's next pull trails.
        _tracked, _owner = rig.tick(demoted, promoted, failures)
        row = pair.get(f"{demoted}/role", "GET /role", failures)
        rows.append(
            {
                "role": row.get("role"),
                "sync": claim_reclaim.sync_state(row),
                "aligned": aligned_tick(row),
                "tick": row.get("tick"),
            }
        )
        for url in (demoted, promoted):
            checkpoint = pair.get(
                f"{url}/checkpoint", "GET /checkpoint", failures
            )
            position = stream_position(checkpoint)
            if position is not None:
                served.append(position)
        time.sleep(FETCH_SETTLE_S)
    phantom = served_restarts(demoted, failures)
    if ctx.get("tamper") == "planted-phantom":
        # The doctored case: a `source_restarted` planted into the
        # demoted peer's same-generation audit — the finding's own
        # phantom, with the no-prior-alignment form its first pull
        # carried.
        phantom = phantom + [
            (None, rows[-1]["tick"] if rows else None, None,
             rows[-1]["aligned"] if rows else None)
        ]
    durable = durable_restarts(ctx["journal"])
    return {
        "rows": rows,
        "served": served,
        "served_phantom": phantom,
        "durable_phantom": [entry for entry in durable
                            if ctx["floor"] is None
                            or entry[0] > ctx["floor"]],
    }


def audit_phantom(name, observed, failures):
    """The same-generation half: an uninterrupted checkpoint stream
    journals no `source_restarted` on either the serving monitor or
    the durable file. One entry is the defect the finding recorded."""
    if observed["served_phantom"]:
        failures.append(
            f"source-restart-evidence-failed: {name} journaled "
            f"{len(observed['served_phantom'])} source_restarted "
            "records on its serving monitor while tracking an "
            "uninterrupted same-generation stream — a phantom "
            "restart the demote-to-track reset manufactures: "
            f"{observed['served_phantom'][:3]}"
        )
    if observed["durable_phantom"]:
        failures.append(
            f"source-restart-evidence-failed: {name} journaled "
            f"{len(observed['durable_phantom'])} source_restarted "
            "records in its durable journal while tracking an "
            "uninterrupted same-generation stream — the peer "
            "attributed its own tracking reset to the source: "
            f"{observed['durable_phantom'][:3]}"
        )
    served = observed["served"]
    for before, after in zip(served, served[1:]):
        if after < before:
            failures.append(
                f"source-restart-evidence-failed: {name}'s served "
                f"checkpoint stream went non-monotone across the "
                f"switch cycle — {served[:12]} — a tracking run "
                "realigned its local tick underneath the line instead "
                "of landing the pull at its own clock"
            )
            break


def audit_restart(name, served, durable, aligned, failures):
    """The genuine-restart half: a cold-restarted source journals
    exactly one `source_restarted` on its tracking peer, in the served
    journal and the durable file alike, carrying the named evidence —
    the prior alignment it broke and the resumed stream tick below
    it."""
    label = f"the tracking peer {name}"
    for kind, entries in (("served journal", served),
                          ("durable journal", durable)):
        if len(entries) != 1:
            failures.append(
                f"source-restart-evidence-failed: {label} journaled "
                f"{len(entries)} source_restarted records in its "
                f"{kind} after the tracked source cold-restarted — a "
                "genuinely cold-restarted source journals exactly one"
            )
            return
    for kind, entries in (("served journal", served),
                          ("durable journal", durable)):
        _seq, _tick, was_aligned, resumed_at = entries[0]
        if not int_tick(was_aligned):
            failures.append(
                f"source-restart-evidence-failed: the {kind}'s entry "
                f"names no prior alignment ({was_aligned!r}) — the "
                "named evidence the contract pins is absent"
            )
        elif not int_tick(resumed_at):
            failures.append(
                f"source-restart-evidence-failed: the {kind}'s entry "
                f"names no resumed stream tick ({resumed_at!r})"
            )
        elif resumed_at >= was_aligned:
            failures.append(
                f"source-restart-evidence-failed: the {kind}'s entry "
                f"resumed at {resumed_at} where its prior alignment "
                f"stood at {was_aligned} — the stream never regressed"
            )
        elif aligned is not None and was_aligned != aligned:
            failures.append(
                f"source-restart-evidence-failed: the {kind}'s entry "
                f"names the prior alignment {was_aligned} where the "
                f"pre-restart stream stood at {aligned}"
            )
    if failures:
        return
    if served != durable:
        failures.append(
            "source-restart-evidence-failed: the served and durable "
            f"journals disagree on the restart {served} vs {durable} "
            "— the durable audit is not the served audit"
        )


def phantom_source_restart_pass(args, tamper):
    """The phantom-source-restart run: converge the declared pair to
    its launch roles, run the demote/promote cycle and audit the
    demoted peer for the phantom the finding recorded, fail the pair
    back and repeat with the roles exchanged, then cold-restart the
    field owner and audit the one `source_restarted` its tracking peer
    owes the boundary. Returns `(digest_entries, evidence, failures)`;
    raises `Inconclusive` where the pinned release predates the
    contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "phantom-source-restart leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    duty_name, standby_name = duty_decl["name"], standby_decl["name"]
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        journals = {
            duty_name: rig.duty_files.get("journal_file"),
            standby_name: rig.standby_files.get("journal_file"),
        }
        for name, path in journals.items():
            if path is None or not os.path.exists(path):
                raise Inconclusive(
                    f"{name} declares no readable journal_file — the "
                    "durable half of the restart audit is absent"
                )

        # Phase 1 — convergence: the declared pair settled on its
        # launch roles, the field owner active and the standby
        # tracking it.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"].get("role"),
                "standby_sync": claim_reclaim.sync_state(
                    converged["standby_role"]
                ),
            }
        )

        # Phase 2 — the contract surface: each peer's served
        # checkpoint must carry the `generation` stamp the
        # suppression reads. An absent surface is the pre-contract
        # shape: inconclusive, never a violation.
        for name, url in ((duty_name, duty_url), (standby_name, standby_url)):
            checkpoint = pair.get(
                f"{url}/checkpoint", "GET /checkpoint", failures
            )
            if not contract_checkpoint(checkpoint):
                raise Inconclusive(
                    f"{name}'s served checkpoint lacks the tick-domain "
                    "contract surface the leg audits (tick, "
                    f"generation): {json.dumps(checkpoint, sort_keys=True)[:250]}"
                )
        digest_entries.append(
            {"phase": "surface", "generation": "declared"}
        )

        # Phase 3 — the demote/promote cycle, audited on the demoted
        # peer: the same-generation stream must journal no restart,
        # and the served stream position must stay monotone while the
        # demoted run realigns its own tick underneath the line.
        duty_floor = max(
            (entry[0] for entry in durable_restarts(journals[duty_name])),
            default=0,
        )
        rig.switch(
            duty_url,
            standby_url,
            failures,
            demote_what="the field owner",
            promote_what="the converged standby",
        )
        forward = cycle(
            {
                "rig": rig,
                "failures": failures,
                "promoted": standby_url,
                "demoted": duty_url,
                "journal": journals[duty_name],
                "floor": duty_floor,
                "scans": CYCLE_SCANS,
                "tamper": tamper,
            }
        )
        audit_phantom(f"the demoted {duty_name}", forward, failures)
        if failures:
            raise Abort
        evidence["forward"] = {
            "rows": forward["rows"],
            "served": forward["served"],
        }
        digest_entries.append(
            {
                "phase": "cycle",
                "rows": len(forward["rows"]),
                "served_monotone": True,
                "phantom": "none",
            }
        )

        # Phase 4 — the fail-back, repeating the cycle with the roles
        # exchanged so each peer's own demotion clears its own
        # alignment and each is the audited tracker once.
        standby_floor = max(
            (entry[0] for entry in durable_restarts(journals[standby_name])),
            default=0,
        )
        restored = rig.switch(
            standby_url,
            duty_url,
            failures,
            demote_what="the promoted peer",
            promote_note=" restoring the launch roles",
        )
        backward = cycle(
            {
                "rig": rig,
                "failures": failures,
                "promoted": duty_url,
                "demoted": standby_url,
                "journal": journals[standby_name],
                "floor": standby_floor,
                "scans": CYCLE_SCANS,
                "tamper": tamper,
            }
        )
        audit_phantom(f"the demoted {standby_name}", backward, failures)
        if failures:
            raise Abort
        evidence["backward"] = {
            "rows": backward["rows"],
            "served": backward["served"],
        }
        digest_entries.append(
            {
                "phase": "failback",
                "rows": len(backward["rows"]),
                "served_monotone": True,
                "phantom": "none",
                "roles": {
                    "demote": restored["demote"].get("role"),
                    "promote": restored["promote"].get("role"),
                },
            }
        )

        # Phase 5 — the genuine restart: the field owner's process
        # stops, its declared state file drops, and a fresh process
        # is respawned on the same declared listen address with no
        # `--state-file` to resume — a new process boot, a new
        # generation, a stream regressed below the tracking peer's
        # alignment. The standby must journal exactly one
        # `source_restarted` carrying its named evidence.
        pre = pair.get(f"{standby_url}/role", "GET /role", failures)
        aligned = aligned_tick(pre)
        if aligned is None:
            raise Inconclusive(
                "the tracking peer serves no stream alignment before "
                f"the restart — GET /role answers {json.dumps(pre)[:200]}"
            )
        evidence["aligned_before"] = aligned
        floor = max(
            (entry[0] for entry in durable_restarts(journals[standby_name])),
            default=0,
        )
        if tamper != "missing-restart":
            pair.stop(rig.duty)
            state_file = rig.duty_files.get("state_file")
            if state_file is not None and os.path.exists(state_file):
                os.remove(state_file)
            rig.duty, duty_url, preamble = pair.spawn_peer(
                args.controller,
                args.model,
                args.dt,
                rig.plant_addr,
                None,
                rig.duty_files,
                listen=duty_url.removeprefix("http://"),
                pair_token=pair.PAIR_TOKEN,
            )
            rig.duty_url = duty_url
            if duty_url is None:
                detail = "; ".join(preamble[-2:]) or "no diagnostic"
                failures.append(
                    f"the cold-restarted field owner exited at "
                    f"startup: {clean_error(detail, rig)}"
                )
                raise Abort
        resumed = None
        for _ in range(RESTART_SCANS):
            pair.scan(standby_url, failures)
            time.sleep(FETCH_SETTLE_S)
            if tamper == "missing-restart":
                pair.scan(duty_url, failures)
                continue
            checkpoint = pair.get(
                f"{duty_url}/checkpoint", "GET /checkpoint", failures
            )
            position = stream_position(checkpoint)
            if position is not None and position < aligned:
                resumed = position
                break
        evidence["resumed_at"] = resumed
        if resumed is None and tamper != "missing-restart":
            raise Inconclusive(
                "the cold-restarted field owner served no regressed "
                f"stream within {RESTART_SCANS} driven scans — the "
                "induction never crossed a run boundary"
            )

        served = [entry for entry in served_restarts(standby_url, failures)
                  if entry[0] is None or entry[0] > floor]
        durable = [entry for entry in durable_restarts(journals[standby_name])
                   if entry[0] is None or entry[0] > floor]
        audit_restart(standby_name, served, durable, aligned, failures)
        if failures:
            raise Abort
        evidence["restart"] = {"served": served, "durable": durable}
        digest_entries.append(
            {
                "phase": "restart",
                "aligned_before": aligned,
                "resumed_at": resumed,
                "served": served,
                "durable": durable,
            }
        )

        # Phase 6 — the launch roles: the respawned owner's startup
        # claim reclaims the field and the standby reconverged
        # `tracking` onto it, the manifest's arrangement the legs
        # behind this one rest on.
        for _ in range(RESTORE_SCANS):
            _tracked, _owner = rig.tick(standby_url, duty_url, failures)
            if tracking(pair.get(f"{duty_url}/role", "GET /role", failures)):
                break
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active":
            failures.append(
                f"the respawned field owner reports "
                f"{duty_role.get('role')!r}, expected active — the "
                "pair was not left in its launch roles"
            )
        if not tracking(standby_role):
            failures.append(
                f"the tracking peer never reconverged after the cold "
                f"restart — GET /role answers {standby_role}"
            )
        if failures:
            raise Abort
        evidence["restored"] = {
            "duty_role": duty_role.get("role"),
            "standby_sync": claim_reclaim.sync_state(standby_role),
        }
        digest_entries.append(
            {
                "phase": "restore",
                "duty_role": duty_role.get("role"),
                "standby_sync": claim_reclaim.sync_state(standby_role),
            }
        )
    except Abort as abort:
        failures.extend(clean_error(arg, rig) if rig is not None
                        else str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {clean_error(error, rig)}")
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
        choices=["planted-phantom", "missing-restart"],
        help="doctor the run — a source_restarted planted into the "
        "demoted peer's same-generation audit, or the cold restart "
        "withheld while the leg asserts its one entry; each must fail "
        "naming the diagnostic",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = phantom_source_restart_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"phantom-source-restart: the {args.tamper} case "
                "wanted the run to surface its named diagnostic — an "
                "inconclusive run offers the doctored case no evidence"
            )
            return 1
        eprint(
            f"phantom-source-restart: inconclusive — {inconclusive}"
        )
        print(
            f"phantom-source-restart-digest inconclusive — "
            f"{inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"phantom-source-restart: {line}")
        return 1
    for failure in failures:
        eprint(f"phantom-source-restart: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"phantom-source-restart: the {args.tamper} case "
                "passed silently — the leg never noticed the doctored "
                "evidence"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"phantom-source-restart-digest {digest} — an orderly "
        f"demote/promote cycle and its fail-back journalled no "
        f"source_restart on either peer's same-generation stream, the "
        f"served checkpoint stream stayed monotone across both, and "
        f"the cold-restarted field owner's tracking peer journalled "
        f"exactly one source_restarted carrying its prior alignment "
        f"and the resumed stream tick"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())