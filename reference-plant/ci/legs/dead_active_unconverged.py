#!/usr/bin/env python3
"""The dead-active unconverged-recovery leg for the reference plant —
the consumer-boundary proof of decision 86's recorded recovery on the
manifest-declared pair (WW-ENG-003, WW-LCM-001 — the rig's
`dead-active-unconverged-recovery` scenario mirrored at the customer
boundary): an active that dies holding the plant's single-writer claim
while its standby is unconverged is recoverable only by
restart-as-active — the conditional startup grant preempting the dead
owner's outliving hold — because promotion requires a converged verdict
a dead peer can never supply.

The pair stage's legs cover the ordered switch, the standby's restart,
and the armed failover — none stages this wedge. This leg, ordered by
the driven harness — a peer's pulls and applies live only inside its
own `POST /scan`, so the episode needs no wall clock:

- launches the declared pair unarmed (manual promotion only — an
  `auto_promote` budget would self-promote the standby out of the
  wedge) and converges it to `tracking`;
- gates the contract surface: the launched active's recorded owner
  token, the fencing verdict's owner attribution, and the declared
  persistence files the restart's resume restores from;
- stops the field owner's controller process mid-claim — the dead
  owner's standing hold keeps fencing foreign mutations under its
  token;
- drives the standby through the dead-peer window: each scan's pull
  misses and its verdict walks unconverged (`degraded`), every
  `POST /promote` answers the named `not_converged` refusal carrying
  that verdict — never a silent promotion — and the monitor keeps
  serving role, snapshot, and journal throughout;
- relaunches the stopped controller as its configured active on its
  declared listen and persistence files: the conditional startup grant
  preempts the dead owner's hold, the run resumes at the persisted
  tick, and the monitor answers `active`;
- drives tracking-first pair ticks until the standby reconverges to a
  promotable verdict — exactly one peer `active` throughout, the
  resumed owner's writes landing and the plant stepping again — and a
  receipted `write_value` settles applied on both peers' logs;
- audits the durable record: the standby's journal carries no role
  transition and no misattributed source restart across the whole
  episode — the warm resume continuing the same tick generation —
  and the duty's journal records the run-2 boundary at the persisted
  tick, the launch roles standing at the end.

The contract postdates early releases: where the launched tooling
predates it the run's own evidence is the pre-contract shape — a
launch preamble holding no field claim, a fencing verdict naming no
owner, a claim that lapses with its dead holder, or a manifest
declaring no persistence for the restart — and the leg reports
`dead-active-unconverged-digest inconclusive` rather than asserting
until the manifest repins a release carrying the contract.

Usage:

    dead_active_unconverged.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `dead-active-unconverged-digest <sha256>` line prints —
the check runs two passes and compares them
(`dead-active-recovery-nondeterministic`). A contract violation
reports `dead-active-unconverged: …` lines on stderr and exits 1 —
the check's `dead-active-recovery-failed`. The `--tamper` cases doctor
the leg's own expectations: `expect-promotion` wants a dead-window
promote admitted and must fail on the named refusal; `skip-relaunch`
never restarts the stopped owner and must fail when no field owner
returns — the check's `dead-active-recovery-unchecked` cover.
"""

import argparse
import json
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import claim_reclaim
import failover
import pair
import refusal
import resume_settle_once
import simulate
import stranded_rejoin


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored cases: a leg asserting the dead-window promote was
# admitted — the defect the convergence gate exists to refuse — and
# a leg that never restarts the stopped owner — the recovery the
# restart-as-active contract performs — must each surface the named
# diagnostic on the honest run rather than passing an unexercised
# contract.
LEG = {
    "order": 620,
    "title": "the dead-active unconverged-recovery leg",
    "passes": "dead-active-recovery",
    "failed": "dead-active-recovery-failed",
    "tampers": [
        {
            "name": "expect-promotion",
            "passed": "an expect-promotion case passed the dead-active-unconverged leg",
            "missed": "the expect-promotion case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted the dead-window promote admitted"],
        },
        {
            "name": "skip-relaunch",
            "passed": "a skip-relaunch case passed the dead-active-unconverged leg",
            "missed": "the skip-relaunch case did not report its named diagnostic",
            "evidence": ["never served its monitor"],
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


# The dead-peer window: one driven standby scan per round walks the
# verdict unconverged, each round probing the refused promote and the
# dead owner's fencing. The recovery bound mirrors the standby-restart
# leg's reconvergence window; the return poll bounds the relaunched
# monitor's answer — under `skip-relaunch` nobody ever answers.
WEDGE_SCANS = 4
RECOVERY_SCANS = 8
RETURN_POLLS = 40
POLL_SLEEP = 0.05


def try_role(url):
    """`GET /role` that answers None on transport error — for the
    wedge's dead-peer and relaunch polls, where a missed answer is
    data, not a harness failure."""
    return claim_reclaim.try_role(url)


def mutation_fenced(verdict):
    """Whether a field-mutation answer is the named fencing refusal —
    `write` carries the point's io-fenced error nested under the `io`
    kind."""
    return claim_reclaim.mutation_fenced(verdict)


def verdict_owner(verdict):
    """The owner token a fencing verdict attributes the standing
    claim to — or None on an unfenced answer or a build predating
    the field."""
    return claim_reclaim.verdict_owner(verdict)


def sync_kind(report):
    """The sync vocabulary a standby's RoleReport carries —
    `tracking`, `orphaned`, `degraded`, `diverged` — or
    `unsynchronized` when the report carries the string form."""
    return stranded_rejoin.sync_kind(report)


def promotable(report):
    """Whether a served RoleReport holds a promotable standby
    posture — tracking, orphaned, or reinitialized: the convergence
    proof `POST /promote` accepts."""
    return resume_settle_once.promotable(report)


def owner_token(preamble):
    """The field-ownership token a launched active's claim line
    reports — None where the release claims only on promotion and
    the dead-owner hold the wedge fences on never forms."""
    return failover.owner_token(preamble)


def writable_bool(model):
    """One writable boolean `in` point the emitted model declares —
    the receipted `write_value` target on the resumed owner."""
    writable = [
        point["id"]
        for point in sorted(model["io_points"], key=lambda entry: entry["id"])
        if point.get("writable") and point["value_type"] == "bool"
    ]
    return writable[0] if writable else None


def write_value(point, boolean):
    """A `write_value` command body for the receipted path."""
    return {
        "write_value": {
            "kind": "bool",
            "point": point,
            "value": {"bool": boolean},
        }
    }


def applied(receipts, command):
    """The applied receipts one submitted command settled into a
    served receipt log."""
    return [
        entry
        for entry in receipts
        if entry.get("command") == command
        and simulate.receipt_outcome(entry) == "applied"
    ]


def resumed_tick(preamble):
    """The tick a `resumed from state file … at tick N` preamble line
    reports — None when the run started cold."""
    for line in preamble:
        resumed = re.search(r"resumed from state file .* at tick (\d+)", line)
        if resumed:
            return int(resumed.group(1))
    return None


def unclaimed(verdict):
    """Whether a probe verdict reports the field unclaimed — the
    pre-contract shape a lapsed dead-owner hold reads as."""
    return failover.probe_kind(verdict) == "unclaimed"


def source_restarts(entries):
    """The `source_restarted` stream a journal entry list carries —
    the regressed-source resync records the standby's monitor
    journaled."""
    return [
        {
            "tick": entry["tick"],
            "was_aligned": restart.get("was_aligned"),
            "resumed_at": restart.get("resumed_at"),
        }
        for entry in entries
        for restart in [entry.get("event", {}).get("source_restarted")]
        if restart is not None
    ]


def dead_active_unconverged_pass(args, tamper):
    """The dead-active run: converge, gate the contract surface, stop
    the field owner, hold the unconverged standby's named refusals
    across the dead-peer window, relaunch the owner as its configured
    active, reconverge the pair, audit, and leave the launch roles
    standing. Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "dead-active-unconverged leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = verdict_io = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io
        with open(args.model) as handle:
            model = json.load(handle)
        points = failover.signal_points(model)
        if points is None:
            raise Abort(
                "the emitted model declares no p101-cmd/level-primary "
                "signal points — the leg has no field output to watch"
            )
        write_point = writable_bool(model)
        if write_point is None:
            raise Abort(
                "the emitted model declares no writable boolean point "
                "— the resumed-owner write has nothing to exercise"
            )
        # The genuinely foreign attachment the fencing probes run on —
        # the run's own plant-protocol client can join a recorded
        # claim's holder set, so it never runs mutation probes.
        verdict_io = simulate.PlantClient(rig.plant_addr)

        # Phase 1 — convergence: the pair settles tracking-first, the
        # configured active owning the field.
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

        # Phase 2 — the contract gates: the launched owner must hold
        # the field claim already (the launch-time hold the dead owner
        # leaves standing), the field's fencing verdict must name it,
        # and the declared persistence must carry the restart's resume.
        duty_token = owner_token(rig.duty_preamble)
        if duty_token is None:
            raise Inconclusive(
                "the launched active's preamble holds no field-claim "
                "line — the pinned release claims only on promotion, "
                "so the dead-owner hold the wedge fences on never forms"
            )
        state_file = rig.duty_files.get("state_file")
        journal_file = rig.duty_files.get("journal_file")
        if not state_file or not journal_file:
            raise Inconclusive(
                "the manifest declares no state/journal files for the "
                "configured active — the restart-as-active recovery's "
                "recorded resume has nothing to relaunch onto"
            )
        probe = failover.foreign_probe(
            verdict_io,
            points["cmd"],
            failover.field_read(plant_io, points["cmd"], failures)["value"],
        )
        if not mutation_fenced(probe):
            failures.append(
                f"a foreign mutation probe on the claimed field "
                f"answered {probe} — the launched owner's hold does "
                "not fence it"
            )
            raise Abort
        if verdict_owner(probe) != duty_token:
            raise Inconclusive(
                "the field's fencing verdict names no owner — the "
                "pinned release predates the claim-attribution shape "
                "the wedge asserts"
            )
        standby_journal_floor = len(
            pair.get(
                f"{standby_url}/journal",
                "the standby's pre-wedge GET /journal",
                failures,
            )
        )
        digest_entries.append(
            {"phase": "gate", "claim": "held", "probe": "fenced"}
        )

        # Phase 3 — the induction: the active dies holding the field's
        # writer claim. Its last persisted checkpoint is the restart's
        # resume point — polled stable since the monitor's drained
        # writer may lag the last driven scan.
        pair.stop(rig.duty)
        rig.duty = None
        persisted = None
        for _ in range(RETURN_POLLS):
            checkpoint = resume_settle_once.state_checkpoint(state_file)
            if isinstance(checkpoint, dict):
                persisted = checkpoint.get("tick")
                if persisted is not None:
                    break
            time.sleep(POLL_SLEEP)
        if persisted != evidence["converged"]:
            failures.append(
                "the stopped owner's persisted checkpoint carries tick "
                f"{persisted}, not the converged tick "
                f"{evidence['converged']} — the declared state file "
                "never caught the run"
            )
            raise Abort
        digest_entries.append(
            {"phase": "stop", "persisted_tick": persisted}
        )

        # Phase 4 — the dead-peer window: each round scans the standby
        # (its pull missing the dead owner), reads its serving
        # monitor's role and journal, and probes a promote that must
        # answer the named not_converged refusal carrying the standing
        # unconverged verdict — while the dead owner's hold keeps
        # fencing foreign mutations under its token.
        wedge = []
        for round_index in range(WEDGE_SCANS):
            snapshot = pair.scan(standby_url, failures)
            report = try_role(standby_url)
            if report is None:
                failures.append(
                    "the standby's monitor stopped serving across the "
                    f"dead-peer window — round {round_index + 1} found "
                    "no /role answer"
                )
                raise Abort
            if report.get("role") != "standby":
                failures.append(
                    "the unconverged standby silently promoted — "
                    f"GET /role answers {report} through the "
                    "dead-peer window"
                )
                raise Abort
            kind = sync_kind(report)
            if kind not in ("degraded", "diverged", "unsynchronized"):
                failures.append(
                    "the standby's sync never walked unconverged "
                    f"across the dead peer — GET /role answers {report}"
                )
                raise Abort
            status, body = pair.request(f"{standby_url}/promote", {})
            if tamper == "expect-promotion":
                if status == 200:
                    continue
                failures.append(
                    "the doctored expectation wanted the dead-window "
                    "promote admitted — the unconverged standby "
                    f"answered {status} {body}"
                )
                raise Abort
            reason = (
                body.get("not_converged")
                if isinstance(body, dict)
                else None
            )
            if status != 409 or not isinstance(reason, dict):
                failures.append(
                    "the dead-window promote did not answer the named "
                    f"not_converged refusal — POST /promote answered "
                    f"{status} {body}"
                )
                raise Abort
            refused_sync = sync_kind({"sync": reason.get("sync")})
            if refused_sync != kind:
                failures.append(
                    "the not_converged refusal carries sync "
                    f"{reason.get('sync')}, not the standby's standing "
                    f"{kind} verdict"
                )
                raise Abort
            held = failover.field_read(
                plant_io, points["cmd"], failures
            )["value"]
            probe = failover.foreign_probe(
                verdict_io, points["cmd"], held
            )
            if unclaimed(probe):
                raise Inconclusive(
                    "the dead owner's claim lapsed with its holder — "
                    "the pinned release predates the outliving-claim "
                    "contract the wedge fences on"
                )
            if not mutation_fenced(probe):
                failures.append(
                    "the dead owner's hold stopped fencing foreign "
                    f"mutations: {probe}"
                )
                raise Abort
            if verdict_owner(probe) != duty_token:
                failures.append(
                    "the field's fencing verdict stopped naming the "
                    f"dead owner's token {duty_token}: {probe}"
                )
                raise Abort
            pair.get(
                f"{standby_url}/journal",
                "the dead-window GET /journal",
                failures,
            )
            wedge.append(
                {
                    "round": round_index + 1,
                    "tick": snapshot["tick"],
                    "sync": kind,
                    "promote": {"status": status, "sync": refused_sync},
                    "probe": "fenced",
                }
            )
        evidence["wedge"] = len(wedge)
        digest_entries.append({"phase": "wedge", "rounds": wedge})

        # Phase 5 — the restart-as-active recovery: the stopped
        # controller relaunches as its configured active on its
        # declared listen and persistence — its conditional startup
        # grant preempting the dead owner's standing hold. Under
        # skip-relaunch nobody returns and the leg must fail naming it.
        claim = None
        if tamper != "skip-relaunch":
            duty_listen = duty_url.removeprefix("http://")
            rig.duty, duty_url, preamble = pair.spawn_peer(
                args.controller,
                args.model,
                args.dt,
                rig.plant_addr,
                None,
                rig.duty_files,
                listen=duty_listen,
                pair_token=pair.PAIR_TOKEN,
            )
            rig.duty_url = duty_url
            if duty_url is None:
                failures.append(
                    "the relaunched controller exited at startup — "
                    f"its preamble reads: "
                    f"{'; '.join(preamble) or 'no diagnostic'}"
                )
                raise Abort
            resumed = resumed_tick(preamble)
            if resumed != persisted:
                failures.append(
                    "the relaunched owner resumed at tick "
                    f"{resumed}, not the persisted tick {persisted} — "
                    "the restart-as-active recovery never restored "
                    "the carried run"
                )
                raise Abort
            evidence["resumed"] = resumed
            claim = owner_token(preamble)
            if claim is None:
                raise Inconclusive(
                    "the relaunched controller held no startup claim — "
                    "the pinned release predates the conditional "
                    "startup grant the recovery preempts with"
                )
            digest_entries.append(
                {"phase": "relaunch", "resumed_tick": resumed}
            )
        report = None
        for _ in range(RETURN_POLLS):
            report = try_role(duty_url)
            if report is not None:
                break
            time.sleep(POLL_SLEEP)
        if report is None:
            failures.append(
                "the relaunched field owner never served its monitor "
                "— the restart-as-active recovery did not run"
            )
            raise Abort
        if report.get("role") != "active":
            failures.append(
                "the relaunched controller did not restart active — "
                f"GET /role answers {report}"
            )
            raise Abort

        # Phase 6 — reconvergence: tracking-first driven ticks until
        # the standby reports a promotable verdict on the relaunched
        # owner — exactly one peer active throughout, the resumed
        # owner's writes landing, the plant stepping again off the
        # frozen level the dead owner left.
        reconverged = None
        ticks = []
        standby_report = None
        level_held = failover.field_read(
            plant_io, points["level"], failures
        )["value"]
        level = level_held
        for _ in range(RECOVERY_SCANS):
            _tracked, owner = pair.tick(
                standby_url,
                duty_url,
                failures,
                diverged="the standby's image diverged from the "
                "relaunched owner's at tick {tick} — the restarted "
                "line never realigned the pair",
            )
            ticks.append(owner["tick"])
            mismatches = refusal.field_mismatches(
                owner, refusal.field_out_samples(plant_io)
            )
            if mismatches:
                failures.append(
                    "the relaunched owner's writes never landed: "
                    + "; ".join(mismatches)
                )
                raise Abort
            standby_report = try_role(standby_url)
            owner_report = try_role(duty_url)
            if standby_report is None or owner_report is None:
                failures.append(
                    "a peer's monitor stopped serving during the "
                    f"recovery — standby {standby_report}, owner "
                    f"{owner_report}"
                )
                raise Abort
            if owner_report.get("role") != "active":
                failures.append(
                    "the relaunched owner stopped reporting active "
                    f"during reconvergence — {owner_report}"
                )
                raise Abort
            if standby_report.get("role") != "standby":
                failures.append(
                    "a second peer reported active during the "
                    f"recovery — {standby_report}"
                )
                raise Abort
            level = failover.field_read(
                plant_io, points["level"], failures
            )["value"]
            if promotable(standby_report):
                reconverged = sync_kind(standby_report)
                break
        if reconverged is None:
            failures.append(
                "the standby never reconverged to a promotable "
                f"verdict inside {RECOVERY_SCANS} driven ticks — "
                f"GET /role answers {standby_report}"
            )
            raise Abort
        if level == level_held:
            failures.append(
                "the field's level never moved after the restart — "
                "the relaunched owner is not stepping the plant it "
                "claims"
            )
            raise Abort
        evidence["reconverged"] = ticks[-1]
        digest_entries.append(
            {
                "phase": "reconverged",
                "ticks": ticks,
                "verdict": reconverged,
            }
        )

        # A receipted write on the resumed owner settles applied and
        # adopts onto the standby's log — the command path recovered
        # with the claim. The operator-writable point lives in the
        # controller's image, not the field — its baseline reads off
        # the owner's served snapshot.
        value = simulate.snapshot_point(owner, write_point)
        boolean = value.get("bool") if isinstance(value, dict) else None
        if boolean is None:
            failures.append(
                f"the writable point {write_point} serves no boolean "
                f"sample in the owner's image: {value}"
            )
            raise Abort
        command = write_value(write_point, not boolean)
        status, receipt = pair.request(
            f"{duty_url}/command",
            {"command": command, "actor": pair.ACTOR},
        )
        if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
            failures.append(
                f"the receipted write {command} on the resumed owner "
                f"answered {status} {receipt}, expected an accepted "
                "receipt"
            )
            raise Abort
        pair.tick(standby_url, duty_url, failures)
        receipts_owner = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        receipts_standby = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if not applied(receipts_owner, command):
            failures.append(
                "the receipted write on the resumed owner never "
                f"settled applied: {receipts_owner}"
            )
            raise Abort
        if receipts_standby != receipts_owner:
            failures.append(
                "the standby's adopted receipt log diverged from the "
                "relaunched owner's — the reconverged pair does not "
                "share one command record"
            )
            raise Abort
        digest_entries.append(
            {"phase": "write", "command": command, "outcome": "applied"}
        )

        # Phase 7 — the audits: the standby's served journal carries no
        # role transition and no misattributed source restart across
        # the whole episode (it never became active, silently or
        # otherwise), the duty's durable journal opens run 2 at the
        # persisted tick, and the field still fences foreign writes
        # under the relaunched owner's token.
        standby_journal = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        episode = standby_journal[standby_journal_floor:]
        transitions = pair.role_transitions(episode)
        if transitions:
            failures.append(
                "the standby's journal carries role transitions "
                f"across the dead-owner episode: {transitions}"
            )
            raise Abort
        # The warm-resumed owner continues its own tick generation —
        # a `source_restarted` entry here would mean the standby read
        # the resumed line as a foreign regressed stream.
        restarts = source_restarts(episode)
        if restarts:
            failures.append(
                "the standby's journal misattributes the warm resume "
                f"as a source restart: {restarts}"
            )
            raise Abort
        records = pair.journal_records(journal_file)
        boundaries = [
            record for kind, record in records if kind == "boundary"
        ]
        if boundaries != [
            {"run": 1, "tick": 0},
            {"run": 2, "tick": persisted},
        ]:
            failures.append(
                f"the relaunched owner's journal boundaries are "
                f"{boundaries}, expected run 1 at tick 0 and run 2 at "
                f"the persisted tick {persisted}"
            )
            raise Abort
        held = failover.field_read(plant_io, points["cmd"], failures)[
            "value"
        ]
        probe = failover.foreign_probe(verdict_io, points["cmd"], held)
        if not mutation_fenced(probe) or verdict_owner(probe) != claim:
            failures.append(
                "the field stopped fencing under the relaunched "
                f"owner's token {claim}: {probe}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "boundaries": boundaries,
                "source_restarts": restarts,
            }
        )

        # Phase 8 — restore: the pair ends on its launch roles — the
        # configured active active, the configured standby a promotable
        # standby — the recovery leaving nothing for later legs to
        # reconcile.
        owner_report = try_role(duty_url)
        standby_report = try_role(standby_url)
        if owner_report is None or owner_report.get("role") != "active":
            failures.append(
                "the configured active did not end the run active — "
                f"{owner_report}"
            )
            raise Abort
        if not promotable(standby_report):
            failures.append(
                "the configured standby did not end the run on a "
                f"promotable verdict — {standby_report}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "restore",
                "duty_tick": owner_report["tick"],
                "standby_verdict": sync_kind(standby_report),
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if verdict_io is not None:
            verdict_io.close()
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
        choices=["expect-promotion", "skip-relaunch"],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = dead_active_unconverged_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "dead-active-unconverged: an inconclusive run under "
                f"the {args.tamper} doctor offers the doctored case "
                "no evidence"
            )
            return 1
        eprint(f"dead-active-unconverged: inconclusive — {inconclusive}")
        print(
            f"dead-active-unconverged-digest inconclusive — {inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"dead-active-unconverged: {line}")
        return 1
    for failure in failures:
        eprint(f"dead-active-unconverged: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"dead-active-unconverged: the {args.tamper} case "
                "passed silently — the leg never noticed the doctored "
                "recovery"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"dead-active-unconverged-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the dead owner's hold fenced "
        f"{evidence['wedge']} refused promote attempts, the relaunched "
        f"owner resumed at tick {evidence['resumed']} and the pair "
        f"reconverged by tick {evidence['reconverged']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
