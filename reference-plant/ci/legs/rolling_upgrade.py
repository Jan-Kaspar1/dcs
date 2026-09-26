#!/usr/bin/env python3
"""The rolling controller-upgrade leg for the reference plant — the
consumer-side proof that the deployed redundant pair rolls its
controller binaries across the recorded release boundary one process
at a time with the field continuously owned (WW-ENG-003, WW-LCM-001 —
decision 27's rolling-upgrade sequence exercised at the customer
boundary on released artifacts).

The pair leg (`ci/legs/pair.py`) proves the manifest-declared pair runs
and switches on the pinned release; the upgrade stage proves the
composition repins and re-emits across the recorded upgrade-from
revision. This leg runs decision 27's sequence on the deployed pair
itself: both controllers launched on the predecessor release's
`dcs-controller` — the stage's `--upgrade-tools` argument resolving the
upgrade-from revision's released tooling through the same per-revision
install mechanism the pinned release's went through — with the plant
on the pinned release's `dcs-plant-server`. The run:

- converges the predecessor pair to `tracking` through the driven-tick
  loop and settles a kind-declared command `applied` into both peers'
  adopted receipt log — the pre-swap half of the run's receipted
  record;
- stops the predecessor standby and relaunches it on the pinned
  release's `dcs-controller` onto its declared
  `--state-file`/`--journal-file`: the preamble reports the resume at
  the persisted tick — the pinned binary consuming the predecessor's
  durable checkpoint — the served journal answers the run-1 entries
  verbatim behind the run-2 boundary, and driven ticks reconverge the
  mixed-release pair to `tracking` while the field owner's writes
  never falter;
- promotes the relaunched peer through the documented
  `demote`/`promote` switch and asserts the new active's writes land:
  the demoted predecessor peer reconverges `tracking` on the pinned
  owner's checkpoint stream — the boundary crossed in the reverse
  direction — the field's stored `out` image stays the promoted
  owner's, and a second receipted command settles exactly once on this
  side of the swap;
- stops the demoted predecessor peer and relaunches it on the pinned
  binary the same way: resumed, rejoined `standby`, reconverged —
  both controllers now pinned, one `active` plus one `tracking`
  standby, the field never unowned;
- restores the pair's launch roles — the manifest's declared duty
  `active` and its standby `tracking`, both on the pinned release —
  and audits the durable record: each journal file's run boundaries
  and `seq` chain intact across the relaunches, each command's
  `command_settled` journaled exactly once, each state file
  checkpointing the run's final tick under the manifest's model
  fingerprint.

Throughout, the plant's own step record — the field `out` points'
stored sample ticks — must advance exactly once per driven owner
scan: a window the harness's tick record can name where the field
stood unowned is the failure this leg exists to name.

A recorded release pair that cannot interoperate — a predecessor
binary that cannot launch the deployment, a pinned relaunch refused
by a named checkpoint-format or model-format refusal reading the
predecessor's artifacts, or a peer's `degraded` negotiation state
naming a version or fingerprint refusal across the boundary — is the
recorded incompatibility, not a leg failure: the run reports
`rolling-upgrade-inconclusive` with the refusal's evidence and exits
0, rather than reporting a false pass.

Usage:

    rolling_upgrade.py --upgrade-controller PATH --plant-server PATH \
        --controller PATH --model model/plant.json \
        --dynamics model/dynamics.json --scenario ci/scenario.json \
        --manifest deploy/manifest.json

On success one `rolling-upgrade-digest <sha256>` line prints — the
check runs two passes and compares them
(`rolling-upgrade-nondeterministic`). A contract violation reports
`rolling-upgrade: …` lines on stderr and exits 1 — the check's
`rolling-upgrade-failed`. `--tamper expect-degraded` doctors the
leg's own expectation — asserting the rolled standby's negotiation
must be refused rather than converge — so the leg proves its
boundary-crossing assertion fires rather than passing an unexercised
crossing.
"""

import argparse
import hashlib
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import refusal
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the rolled standby's
# negotiation must refuse must surface the named diagnostic on
# the honest converged crossing — never a silently unexercised
# boundary.
LEG = {
    "order": 310,
    "title": "the rolling controller-upgrade leg",
    "passes": "rolling-upgrade-leg",
    "upgrade_tools": {
        "upgrade-controller": "dcs-controller",
    },
    "tampers": [
        {
            "name": "expect-degraded",
            "passed": "a expect-degraded case passed the rolling-upgrade leg",
            "missed": "the expect-degraded case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted a refused negotiation"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The recorded release pair cannot interoperate — a named
    checkpoint-format or model-format refusal across the boundary is
    evidence of the recorded incompatibility, never a pass."""


# The driven ticks each window runs: the predecessor pair's
# convergence, the downtime windows the surviving owner keeps stepping
# through while its peer is down, the bounded reconvergence window a
# rolled peer must report `tracking` inside, and each switch's
# handover. The actor the leg's receipted submissions declare.
CONVERGE_TICKS = 4
DOWNTIME_TICKS = 3
ROLL_TICKS = 8
HANDOVER_TICKS = 4
ACTOR = "ci-rolling-upgrade"

# The named-refusal vocabulary a cross-release checkpoint or model
# boundary speaks: a peer's `degraded` detail or a relaunch preamble
# carrying one of these is the recorded incompatibility — the
# checkpoint format version floor, the model-fingerprint negotiation,
# the model-boundary carryover refusal, or the model document's own
# version refusal.
INCOMPATIBLE_MARKERS = (
    "unsupported model version",
    "unsupported checkpoint format version",
    "model fingerprint",
    "model-boundary carryover",
)


def incompatible(detail):
    """Whether a refusal detail names a checkpoint-format or
    model-format incompatibility — the recorded boundary refusal the
    leg classifies inconclusive."""
    return any(marker in detail for marker in INCOMPATIBLE_MARKERS)


def role(url, failures):
    """The peer's served RoleReport."""
    return pair.get(f"{url}/role", "GET /role", failures)


def tracking(report):
    """Whether a RoleReport reads `standby` under `tracking` sync."""
    sync = report.get("sync")
    return report.get("role") == "standby" and (
        isinstance(sync, dict) and "tracking" in sync
    )


def degraded_detail(report):
    """The `degraded` sync detail a RoleReport carries, or None."""
    sync = report.get("sync")
    if isinstance(sync, dict) and "degraded" in sync:
        body = sync.get("degraded") or {}
        return body.get("detail") or ""
    return None


def field_tick(plant_io):
    """The plant's own step record: the latest field `out` sample tick
    — the step counter every field-owning scan advances exactly once."""
    samples = refusal.field_out_samples(plant_io)
    ticks = [
        sample["tick"]
        for sample in samples.values()
        if isinstance(sample, dict) and isinstance(sample.get("tick"), int)
    ]
    if not ticks:
        raise Abort(
            "the simulated plant serves no stepped out-point sample — "
            "the field-ownership record is unobservable"
        )
    return max(ticks)


def owner_scan(rig, url, steps, failures):
    """One driven scan on the field-owning peer: the plant's step
    record must advance exactly once — the harness's tick record names
    any driven window the field stood unowned inside."""
    owner = pair.scan(url, failures)
    steps["expected"] += 1
    got = field_tick(rig.plant_io)
    if got != steps["expected"]:
        failures.append(
            f"the plant's step record reads {got} after owner tick "
            f"{owner['tick']}, expected {steps['expected']} — the "
            "field stood unowned inside a driven window"
        )
        raise Abort
    return owner


def boundary_tick(rig, tracked_url, owner_url, steps, failures,
                  watch=None, diverged=None):
    """One tracking-first pair tick — `pair.tick`'s own ordering and
    assertions plus the leg's two audits. `watch`, when given as
    `(url, what)`, classifies the tracked peer's degraded report after
    its pull: a named format refusal is inconclusive, never a failed
    divergence. The plant's step record must advance exactly once with
    the owner's scan. Returns `(tracked, owner)`."""
    tracked = pair.scan(tracked_url, failures)
    if watch is not None:
        url, what = watch
        detail = degraded_detail(role(url, failures))
        if detail is not None and incompatible(detail):
            raise Inconclusive(
                f"{what} reports the recorded boundary refusal: {detail}"
            )
    owner = owner_scan(rig, owner_url, steps, failures)
    if pair.select_snapshot(tracked) != pair.select_snapshot(owner):
        # The carried-command one-tick lag (issue #689): one more
        # tracking-first pair tick must converge — anything still
        # diverged is the recorded wording's failure.
        tracked = pair.scan(tracked_url, failures)
        if watch is not None:
            url, what = watch
            detail = degraded_detail(role(url, failures))
            if detail is not None and incompatible(detail):
                raise Inconclusive(
                    f"{what} reports the recorded boundary refusal: "
                    f"{detail}"
                )
        owner = owner_scan(rig, owner_url, steps, failures)
        if pair.select_snapshot(tracked) != pair.select_snapshot(owner):
            failures.append(
                (
                    diverged
                    or "the tracking peer's image diverged from the "
                    "field owner's at tick {tick}"
                ).format(tick=owner["tick"])
            )
            raise Abort
    if tracked["tick"] < owner["tick"]:
        failures.append(
            f"the tracking peer's run tick {tracked['tick']} lags the "
            f"field owner's {owner['tick']} after a tracking-first "
            "pair tick — a tracking apply lands at the later of the "
            "two clocks, so the tracker can never trail the stream "
            "it just applied"
        )
        raise Abort
    return tracked, owner


def converge(rig, steps, failures, count=CONVERGE_TICKS):
    """The standby-convergence phase on boundary ticks — `count`
    driven ticks scanning the tracking peer first, the peers resting
    at identical images, the plant stepping once per owner scan — then
    the role reports. Returns the converge record."""
    ticks = []
    for _ in range(count):
        _tracked, owner = boundary_tick(
            rig, rig.standby_url, rig.duty_url, steps, failures
        )
        ticks.append(owner["tick"])
    standby_role = role(rig.standby_url, failures)
    duty_role = role(rig.duty_url, failures)
    if not tracking(standby_role):
        failures.append(
            "the tracking peer never reported tracking — GET /role "
            f"answers {standby_role}"
        )
        raise Abort
    if duty_role.get("role") != "active":
        failures.append(
            f"the field owner reports {duty_role.get('role')!r}, "
            "expected active"
        )
        raise Abort
    return {
        "ticks": ticks,
        "owner": owner,
        "duty_role": duty_role,
        "standby_role": standby_role,
    }


def declared_command(url, failures):
    """One `(component, spec, command)` the peer's served registry
    declares natively — the `invoke` the leg submits."""
    schema = pair.get(f"{url}/schema", "GET /schema", failures)
    declared = pair.declared_command(schema)
    if declared is None:
        failures.append(
            "the served registry declares no command — the leg's "
            "receipted path has nothing to exercise"
        )
        raise Abort
    component, spec = declared
    command = {
        "invoke": {
            "component": component,
            "command": spec["name"],
            "arguments": simulate.command_arguments(spec),
        }
    }
    return component, spec, command


def submit(url, command, failures):
    """`POST /command` asserting an accepted receipt; returns it."""
    status, receipt = pair.request(
        f"{url}/command", {"command": command, "actor": ACTOR}
    )
    if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
        failures.append(
            f"the declared command answered {status} {receipt}, "
            "expected an accepted receipt"
        )
        raise Abort
    return receipt


def settlements(journal, command):
    """The `(seq, tick, outcome)` of a served journal's
    `command_settled` entries whose receipt answers `command` under
    the leg's actor — the exactly-once audit."""
    found = []
    for entry in journal:
        settled = entry.get("event", {}).get("command_settled")
        if settled is None:
            continue
        receipt = settled.get("receipt", {})
        if (
            receipt.get("command") == command
            and receipt.get("actor") == ACTOR
        ):
            found.append(
                (entry["seq"], entry["tick"],
                 simulate.receipt_outcome(receipt))
            )
    return found


def applied(receipts, command):
    """The applied receipts one submitted command settled into a
    served receipt log."""
    return [
        entry
        for entry in receipts
        if entry.get("command") == command
        and simulate.receipt_outcome(entry) == "applied"
    ]


def assert_one_settle(url, command, label, failures):
    """Exactly one `command_settled` for `command` on the peer's
    served journal — and that one `applied` — the exactly-once audit
    each side of the swap owes."""
    journal = pair.get(f"{url}/journal", "GET /journal", failures)
    found = settlements(journal, command)
    if len(found) != 1 or found[0][2] != "applied":
        failures.append(
            f"{label} carries {found} for the receipted command, "
            "expected exactly one applied settlement"
        )
        raise Abort
    return found[0]


def field_check(owner, plant_io, failures, when):
    """The field-mismatch audit one owner snapshot leaves — every
    simulated `out` point holding the owner's served image."""
    field = refusal.field_out_samples(plant_io)
    for mismatch in refusal.field_mismatches(owner, field):
        failures.append(f"{mismatch} {when} — the active's field writes faltered")
    if failures:
        raise Abort
    return field


def resume_evidence(preamble, persisted, what, failures):
    """The relaunch's resume proof: the preamble must report the
    state-file resume at the persisted tick — never a silent cold
    start over the predecessor's durable checkpoint."""
    line = next(
        (line for line in preamble if "resumed from state file" in line),
        None,
    )
    if line is None:
        failures.append(
            f"the relaunched {what} never reported a resume — its "
            "cold start at tick 0 silently abandons the persisted "
            f"run at tick {persisted}"
        )
        raise Abort
    match = re.search(r"at tick (\d+)", line)
    resumed = int(match.group(1)) if match else None
    if resumed != persisted:
        failures.append(
            f"the relaunched {what} resumed at tick {resumed}, the "
            f"persisted checkpoint stood at {persisted}"
        )
        raise Abort
    return resumed


def roll_peer(rig, attr, url_attr, binary, target, files, args, failures):
    """Relaunch a stopped rig peer on the pinned `binary` wired
    `--standby <target>` onto the same declared persistence files —
    the one-process-at-a-time binary swap. A startup refusal
    classifies: a named format refusal reading the predecessor's
    artifacts is the recorded incompatibility; any other exit is a
    leg failure. Returns the spawn preamble."""
    process, url, preamble = pair.spawn_peer(
        binary,
        args.model,
        args.dt,
        rig.plant_addr,
        target,
        files,
        pair_token=pair.PAIR_TOKEN,
    )
    setattr(rig, attr, process)
    setattr(rig, url_attr, url)
    if url is None:
        detail = "; ".join(preamble[-3:]) or "no diagnostic"
        if incompatible(detail):
            raise Inconclusive(
                "the pinned relaunch refused the predecessor's "
                f"artifacts: {detail}"
            )
        failures.append(
            f"the relaunched {attr} exited at startup: {detail}"
        )
        raise Abort
    return preamble


def rejoin(url, failures):
    """The relaunched peer's rejoin assertions: `standby`
    unsynchronized — never a field claim — and the durable journal's
    run-2 boundary ordering the file's record."""
    report = role(url, failures)
    if report.get("role") != "standby" or (
        report.get("sync") != "unsynchronized"
    ):
        failures.append(
            f"the relaunched peer reports {report} — expected "
            "standby/unsynchronized: it must rejoin in standby, "
            "never claiming the field"
        )
        raise Abort
    return report


def replay_check(url, served, persisted, failures):
    """The relaunched peer's served journal must answer the
    pre-relaunch entries verbatim behind the run-2 boundary entry —
    the run's journal record unbroken across the binary swap."""
    replayed = pair.get(f"{url}/journal", "GET /journal", failures)
    if replayed[: len(served)] != served:
        failures.append(
            "the relaunched peer's replayed journal no longer answers "
            "the pre-relaunch entries verbatim"
        )
        raise Abort
    boundary = {
        "seq": len(served) + 1,
        "tick": persisted,
        "event": {"run_boundary": {"run": 2}},
    }
    if replayed[len(served) :] != [boundary]:
        failures.append(
            "the relaunched peer's served journal does not open with "
            f"the run-2 boundary entry {boundary}: "
            f"{replayed[len(served):]}"
        )
        raise Abort


def persisted_checkpoint(state_path, stopped_tick, fingerprint, what, failures):
    """The stopped peer's durable checkpoint: the declared state file
    must hold the run's persisted tick under the manifest's model
    fingerprint — the artifact the pinned binary next consumes."""
    if not os.path.exists(state_path):
        failures.append(
            f"the tracking run left no state file at {state_path}"
        )
        raise Abort
    try:
        with open(state_path) as handle:
            checkpoint = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        failures.append(
            f"the {what}'s persisted state file does not parse: {error}"
        )
        raise Abort
    persisted = checkpoint.get("tick")
    if persisted != stopped_tick:
        failures.append(
            f"the {what}'s state file persisted tick {persisted} "
            f"while the run stood at {stopped_tick}"
        )
        raise Abort
    if checkpoint.get("model_fingerprint") != fingerprint:
        failures.append(
            f"the {what}'s state file carries fingerprint "
            f"{checkpoint.get('model_fingerprint')}, the manifest "
            f"declares {fingerprint}"
        )
        raise Abort
    return persisted


def reconverge(rig, tracked_url, owner_url, steps, what, failures):
    """Drive tracking-first pair ticks until the relaunched peer
    reports `tracking` — bounded by ROLL_TICKS, the tracked peer's
    boundary refusal classifying inconclusive on the way. Returns the
    tracking RoleReport."""
    report = None
    for _ in range(ROLL_TICKS):
        boundary_tick(
            rig, tracked_url, owner_url, steps, failures,
            watch=(tracked_url, what),
        )
        report = role(tracked_url, failures)
        if tracking(report):
            return report
    detail = degraded_detail(report)
    if detail is not None and incompatible(detail):
        raise Inconclusive(
            f"{what} reports the recorded boundary refusal: {detail}"
        )
    failures.append(
        f"{what} never reported tracking inside the leg's declared "
        f"reconvergence window — GET /role answers {report}"
    )
    raise Abort


def identical_receipts(url_a, url_b, failures):
    """Both peers' adopted receipt logs must read as one log."""
    receipts_a = pair.get(f"{url_a}/receipts", "GET /receipts", failures)
    receipts_b = pair.get(f"{url_b}/receipts", "GET /receipts", failures)
    if receipts_a != receipts_b:
        failures.append(
            "the peers' receipt logs diverged — the adopted audit is "
            "not one log"
        )
        raise Abort
    return receipts_a


def rolling_upgrade_pass(args, tamper):
    """The rolling-upgrade run: predecessor pair, standby roll,
    swap, duty roll, restore — with the field's step record unbroken.
    Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "rolling-upgrade leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    writable = [
        point["id"]
        for point in sorted(model["io_points"], key=lambda entry: entry["id"])
        if point.get("writable") and point["value_type"] == "bool"
    ]
    if not writable:
        raise Abort(
            "the emitted model declares no writable boolean point — "
            "the rolling-upgrade leg's second command has nothing to "
            "exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        # Phase 1 — the recorded predecessor pair: both peers launched
        # on the upgrade-from release's `dcs-controller`, the plant on
        # the pinned release's `dcs-plant-server`. A predecessor
        # member that cannot run the deployment at all cannot
        # interoperate as a pair — inconclusive, never a launch
        # failure of the pinned release.
        try:
            rig = pair.launch_pair(
                args, declared, controller=args.upgrade_controller
            )
        except Abort as abort:
            raise Inconclusive(
                "the recorded predecessor release cannot run the "
                "deployment: "
                + "; ".join(str(arg) for arg in abort.args)
            )
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_files, standby_files = rig.duty_files, rig.standby_files
        steps = {"expected": field_tick(rig.plant_io)}

        converged = converge(rig, steps, failures)
        owner = converged["owner"]
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # Phase 2 — the pre-swap receipted command: a kind-declared
        # invoke submitted to the predecessor active settles `applied`
        # exactly once into both peers' adopted log — the record the
        # roll must not break.
        component, spec, command_a = declared_command(duty_url, failures)
        receipt_a = submit(duty_url, command_a, failures)
        _tracked, owner = boundary_tick(
            rig, standby_url, duty_url, steps, failures
        )
        for _ in range(5):
            if tracking(role(standby_url, failures)):
                break
            owner = owner_scan(rig, duty_url, steps, failures)
            _tracked, owner = boundary_tick(
                rig, standby_url, duty_url, steps, failures
            )
        else:
            failures.append(
                "the tracking peer never reconverged after the "
                "command's settling tick"
            )
            raise Abort
        receipts = identical_receipts(duty_url, standby_url, failures)
        if not applied(receipts, command_a):
            failures.append(
                "the declared command never settled applied into the "
                "adopted receipt log"
            )
            raise Abort
        settled_a_duty = assert_one_settle(
            duty_url, command_a, "the field owner's journal", failures
        )
        settled_a_standby = assert_one_settle(
            standby_url, command_a, "the tracking peer's journal", failures
        )
        evidence["command_a_at"] = settled_a_duty[1]
        digest_entries.append(
            {
                "phase": "command-before",
                "component": component,
                "command": spec["name"],
                "receipt": receipt_a,
                "settled": settled_a_duty,
                "adopted": settled_a_standby,
            }
        )

        # Phase 3 — roll the standby: the predecessor standby stops,
        # the pinned binary relaunches onto its declared files, and
        # the mixed-release pair reconverges — the predecessor owner
        # stepping the field through the whole window.
        stopped = pair.get(
            f"{standby_url}/snapshot", "GET /snapshot", failures
        )
        served_standby = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        persisted = persisted_checkpoint(
            standby_files["state_file"],
            stopped["tick"],
            rig.fingerprint,
            "standby",
            failures,
        )
        pair.stop(rig.standby)
        rig.standby = None
        downtime = []
        for _ in range(DOWNTIME_TICKS):
            owner = owner_scan(rig, duty_url, steps, failures)
            field = field_check(
                owner, rig.plant_io, failures,
                "while the standby was down",
            )
            downtime.append({"tick": owner["tick"], "field": field})
        preamble = roll_peer(
            rig, "standby", "standby_url", args.controller,
            duty_url.removeprefix("http://"), standby_files, args,
            failures,
        )
        standby_url = rig.standby_url
        resumed = resume_evidence(
            preamble, persisted, "standby", failures
        )
        rejoin(standby_url, failures)
        replay_check(standby_url, served_standby, persisted, failures)
        tracked_role = reconverge(
            rig, standby_url, duty_url, steps,
            "the relaunched standby", failures,
        )
        if tamper == "expect-degraded":
            failures.append(
                f"the relaunched standby reports {tracked_role} — the "
                "doctored expectation wanted a refused negotiation, "
                "the honest crossing tracked"
            )
            raise Abort
        receipts = identical_receipts(duty_url, standby_url, failures)
        if not applied(receipts, command_a):
            failures.append(
                "the adopted receipt log lost the settled command "
                "across the standby's roll"
            )
            raise Abort
        evidence["standby_rolled"] = resumed
        digest_entries.append(
            {
                "phase": "standby-roll",
                "persisted": persisted,
                "resumed": resumed,
                "downtime": downtime,
                "tracked_role": tracked_role,
            }
        )

        # Phase 4 — the swap: demote the predecessor field owner and
        # promote the relaunched pinned peer, the handover ticks
        # scanning the demoted peer first while it tracks the pinned
        # owner's checkpoint stream — the boundary crossed in the
        # reverse direction. The new active's writes land: a second
        # receipted command settles exactly once.
        demote = rig.demote(
            duty_url, failures, "the predecessor-release field owner"
        )
        promote = rig.promote(
            standby_url, failures, "the relaunched pinned peer"
        )
        handover = []
        for _ in range(HANDOVER_TICKS):
            _tracked, owner = boundary_tick(
                rig, duty_url, standby_url, steps, failures,
                watch=(duty_url, "the demoted predecessor peer"),
                diverged="the demoted peer's image diverged from the "
                "promoted owner's at tick {tick} — the swap was not "
                "bumpless",
            )
            handover.append(owner["tick"])
        promoted_role = role(standby_url, failures)
        if promoted_role.get("role") != "active":
            failures.append(
                f"the promoted peer reports "
                f"{promoted_role.get('role')!r}, expected active"
            )
            raise Abort
        demoted_role = role(duty_url, failures)
        detail = degraded_detail(demoted_role)
        if detail is not None and incompatible(detail):
            raise Inconclusive(
                "the demoted predecessor peer reports the recorded "
                f"boundary refusal: {detail}"
            )
        if not tracking(demoted_role):
            failures.append(
                "the demoted predecessor peer never reconverged on "
                f"the pinned owner — GET /role answers {demoted_role}"
            )
            raise Abort
        field = field_check(
            owner, rig.plant_io, failures, "after the swap"
        )
        point = writable[0]
        before = simulate.snapshot_point(owner, point)
        command_b = {
            "write_value": {
                "kind": "bool",
                "point": point,
                "value": {"bool": not before["bool"]},
            }
        }
        receipt_b = submit(standby_url, command_b, failures)
        _tracked, owner = boundary_tick(
            rig, duty_url, standby_url, steps, failures,
            watch=(duty_url, "the demoted predecessor peer"),
        )
        settled_b_new = assert_one_settle(
            standby_url, command_b, "the promoted peer's journal",
            failures,
        )
        receipts = identical_receipts(duty_url, standby_url, failures)
        if not applied(receipts, command_b):
            failures.append(
                "the promoted owner's receipted command never settled "
                "applied into the adopted log"
            )
            raise Abort
        for url, name in (
            (duty_url, "the demoted peer"),
            (standby_url, "the promoted peer"),
        ):
            journal = pair.get(f"{url}/journal", "GET /journal", failures)
            for command, label in (
                (command_a, "the pre-swap command"),
                (command_b, "the post-swap command"),
            ):
                found = settlements(journal, command)
                if len(found) != 1 or found[0][2] != "applied":
                    failures.append(
                        f"{name}'s journal carries {found} for "
                        f"{label}, expected exactly one applied "
                        "settlement"
                    )
        if failures:
            raise Abort
        evidence["promoted"] = demote["tick"]
        evidence["command_b_at"] = settled_b_new[1]
        digest_entries.append(
            {
                "phase": "swap",
                "demote": demote,
                "promote": promote,
                "ticks": handover,
                "promoted_role": promoted_role,
                "demoted_role": demoted_role,
                "command": command_b,
                "receipt": receipt_b,
                "settled": settled_b_new,
                "field": field,
            }
        )

        # Phase 5 — roll the remaining predecessor peer: the demoted
        # duty controller stops and relaunches on the pinned binary
        # wired at the promoted owner, onto its declared files. The
        # pair reconverges to one active plus one tracking standby —
        # all-pinned now — the field's step record still unbroken.
        stopped_duty = pair.get(
            f"{duty_url}/snapshot", "GET /snapshot", failures
        )
        served_duty = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        persisted_duty = persisted_checkpoint(
            duty_files["state_file"],
            stopped_duty["tick"],
            rig.fingerprint,
            "duty",
            failures,
        )
        pair.stop(rig.duty)
        rig.duty = None
        downtime_duty = []
        for _ in range(DOWNTIME_TICKS):
            owner = owner_scan(rig, standby_url, steps, failures)
            field = field_check(
                owner, rig.plant_io, failures,
                "while the second peer was down",
            )
            downtime_duty.append({"tick": owner["tick"], "field": field})
        preamble_duty = roll_peer(
            rig, "duty", "duty_url", args.controller,
            standby_url.removeprefix("http://"), duty_files, args,
            failures,
        )
        duty_url = rig.duty_url
        resumed_duty = resume_evidence(
            preamble_duty, persisted_duty, "duty", failures
        )
        rejoin(duty_url, failures)
        replay_check(duty_url, served_duty, persisted_duty, failures)
        tracked_duty = reconverge(
            rig, duty_url, standby_url, steps,
            "the second relaunched peer", failures,
        )
        active_role = role(standby_url, failures)
        if active_role.get("role") != "active":
            failures.append(
                f"the field owner reports "
                f"{active_role.get('role')!r} after the second roll, "
                "expected active — the promoted peer lost the field"
            )
            raise Abort
        receipts = identical_receipts(duty_url, standby_url, failures)
        for command, label in (
            (command_a, "the pre-swap command"),
            (command_b, "the post-swap command"),
        ):
            if not applied(receipts, command):
                failures.append(
                    f"the adopted receipt log lost {label} across "
                    "the second roll"
                )
        if failures:
            raise Abort
        evidence["duty_rolled"] = resumed_duty
        digest_entries.append(
            {
                "phase": "duty-roll",
                "persisted": persisted_duty,
                "resumed": resumed_duty,
                "downtime": downtime_duty,
                "tracked_role": tracked_duty,
                "active_role": active_role,
            }
        )

        # Phase 6 — restore the pair's launch roles on the pinned
        # release: demote the promoted peer, promote the second
        # relaunched peer — the manifest's declared duty back to
        # `active`, its declared standby back to `tracking`.
        demote_restore = rig.demote(
            standby_url, failures, "the promoted peer"
        )
        promote_restore = rig.promote(
            duty_url, failures, "the second relaunched peer"
        )
        restore_ticks = []
        for _ in range(HANDOVER_TICKS):
            _tracked, owner = boundary_tick(
                rig, standby_url, duty_url, steps, failures,
                watch=(standby_url, "the demoted pinned peer"),
                diverged="the demoted peer's image diverged from the "
                "restored owner's at tick {tick} — the restore was "
                "not bumpless",
            )
            restore_ticks.append(owner["tick"])
        restored_role = role(duty_url, failures)
        if restored_role.get("role") != "active":
            failures.append(
                f"the restored duty reports "
                f"{restored_role.get('role')!r}, expected active"
            )
            raise Abort
        demoted_restore = role(standby_url, failures)
        detail = degraded_detail(demoted_restore)
        if detail is not None and incompatible(detail):
            raise Inconclusive(
                "the demoted peer reports the recorded boundary "
                f"refusal: {detail}"
            )
        if not tracking(demoted_restore):
            failures.append(
                "the demoted peer never reconverged after the "
                f"restore — GET /role answers {demoted_restore}"
            )
            raise Abort
        field = field_check(
            owner, rig.plant_io, failures, "after the restore"
        )
        receipts = identical_receipts(duty_url, standby_url, failures)
        evidence["restored"] = demote_restore["tick"]
        digest_entries.append(
            {
                "phase": "restore",
                "demote": demote_restore,
                "promote": promote_restore,
                "ticks": restore_ticks,
                "restored_role": restored_role,
                "demoted_role": demoted_restore,
                "field": field,
            }
        )

        # Phase 7 — the record: each peer's durable journal file
        # carries the run's record across its relaunch — the cold-start
        # and resume boundaries in order, the `seq` chain 1..n intact,
        # the role transitions the roll journals, both commands'
        # settlements exactly once — and each declared state file
        # checkpoints the run's final tick under the manifest's model
        # fingerprint.
        final_tick = owner["tick"]
        expected_transitions = {
            duty_decl["name"]: [
                ("active", "demoting"),
                ("demoting", "standby"),
                ("standby", "promoting"),
                ("promoting", "active"),
            ],
            standby_decl["name"]: [
                ("standby", "promoting"),
                ("promoting", "active"),
                ("active", "demoting"),
                ("demoting", "standby"),
            ],
        }
        persisted_at = {
            duty_decl["name"]: persisted_duty,
            standby_decl["name"]: persisted,
        }
        files = {
            duty_decl["name"]: (duty_files, duty_url),
            standby_decl["name"]: (standby_files, standby_url),
        }
        persisted_record = {}
        for name in (duty_decl["name"], standby_decl["name"]):
            peer_files, url = files[name]
            journal_path = peer_files.get("journal_file")
            state_path = peer_files.get("state_file")
            record = {}
            if journal_path is None:
                failures.append(
                    f"{name} declares no journal_file — the "
                    "rolling-upgrade leg's record audit has nothing "
                    "to read"
                )
            elif not os.path.exists(journal_path):
                failures.append(
                    f"{name}'s declared journal file {journal_path} "
                    "does not exist — the --journal-file flag was "
                    "not honored"
                )
            else:
                records = pair.journal_records(journal_path)
                boundaries = [
                    record_
                    for kind, record_ in records
                    if kind == "boundary"
                ]
                want_boundaries = [
                    {"run": 1, "tick": 0},
                    {"run": 2, "tick": persisted_at[name]},
                ]
                if boundaries != want_boundaries:
                    failures.append(
                        f"{name}'s journal boundaries are {boundaries}, "
                        f"expected {want_boundaries}"
                    )
                entries = [
                    record_
                    for kind, record_ in records
                    if kind == "entry"
                ]
                seqs = [entry["seq"] for entry in entries]
                if seqs != list(range(1, len(seqs) + 1)):
                    failures.append(
                        f"{name}'s journal seqs are not 1..n in "
                        f"order: {seqs}"
                    )
                transitions = [
                    (frm, to)
                    for _tick, frm, to in pair.role_transitions(entries)
                ]
                if transitions != expected_transitions[name]:
                    failures.append(
                        f"{name}'s journal file carries the role "
                        f"transitions {transitions}, expected "
                        f"{expected_transitions[name]}"
                    )
                record["journal_records"] = records
            if state_path is None:
                failures.append(
                    f"{name} declares no state_file — the "
                    "rolling-upgrade leg's persistence audit has "
                    "nothing to read"
                )
            elif not os.path.exists(state_path):
                failures.append(
                    f"{name}'s declared state file {state_path} does "
                    "not exist — the --state-file flag was not honored"
                )
            else:
                try:
                    with open(state_path) as handle:
                        checkpoint = json.load(handle)
                except (OSError, json.JSONDecodeError) as error:
                    failures.append(
                        f"{name}'s state file does not parse: {error}"
                    )
                    checkpoint = None
                if checkpoint is not None:
                    if checkpoint.get("tick") != final_tick:
                        failures.append(
                            f"{name}'s state file persisted tick "
                            f"{checkpoint.get('tick')} while the run "
                            f"stood at {final_tick}"
                        )
                    if (
                        checkpoint.get("model_fingerprint")
                        != rig.fingerprint
                    ):
                        failures.append(
                            f"{name}'s state file carries fingerprint "
                            f"{checkpoint.get('model_fingerprint')}, "
                            f"the manifest declares {rig.fingerprint}"
                        )
                    record["state_tick"] = checkpoint.get("tick")
            persisted_record[name] = record
        # The served-journal transition audit beside the file's: each
        # peer answers the same ordered record.
        for name, (_peer_files, url) in files.items():
            transitions = [
                (frm, to)
                for _tick, frm, to in rig.served_transitions(url, failures)
            ]
            if transitions != expected_transitions[name]:
                failures.append(
                    f"{name}'s served journal carries the role "
                    f"transitions {transitions}, expected "
                    f"{expected_transitions[name]}"
                )
        if failures:
            raise Abort
        digest_entries.append(
            {"phase": "record", "persisted": persisted_record}
        )
        evidence["final_tick"] = final_tick
        evidence["steps"] = steps["expected"]
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
    parser.add_argument(
        "--upgrade-controller",
        required=True,
        help="the recorded upgrade-from release's dcs-controller — "
        "the predecessor binary the pair launches on",
    )
    parser.add_argument("--plant-server", required=True)
    parser.add_argument("--controller", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--tamper",
        choices=["expect-degraded"],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the honest converged crossing",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = rolling_upgrade_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "rolling-upgrade: the doctored expectation wanted a "
                "refused negotiation — an inconclusive run offers "
                "the doctored case no evidence"
            )
            return 1
        eprint(f"rolling-upgrade: inconclusive — {inconclusive}")
        print(f"rolling-upgrade-inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"rolling-upgrade: {line}")
        return 1
    for failure in failures:
        eprint(f"rolling-upgrade: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"rolling-upgrade: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    records = digest_entries[-1]["persisted"]
    counts = "+".join(
        str(len(record.get("journal_records", [])))
        for record in records.values()
    )
    print(
        f"rolling-upgrade-digest {digest} — the predecessor pair "
        f"tracked by tick {evidence['converged']}, command settled at "
        f"tick {evidence['command_a_at']}, the standby rolled and "
        f"resumed at tick {evidence['standby_rolled']}, promoted at "
        f"tick {evidence['promoted']} with the second command settled "
        f"at tick {evidence['command_b_at']}, the duty peer rolled "
        f"and resumed at tick {evidence['duty_rolled']}, launch roles "
        f"restored at tick {evidence['restored']}, the field stepped "
        f"through all {evidence['steps']} owner scans, "
        f"{counts} persisted journal records"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
