#!/usr/bin/env python3
"""The diverged-field wedge-recovery leg for the reference plant — the
consumer-boundary mirror of the rig-side wedge-recovery leg, pinning
decision 94's recorded remedy and decision 90's unclaimed-field
signal on the customer-owned pair (WW-ENG-003, WW-LCM-001).

The wedge this leg stages is the one the rig reproduced under QA
finding `diverged-field-wedge-no-controller-recovery`: a foreign
attachment runs the protocol-legal sequence `claim_writer` (which
preempts unconditionally), one field `Out` write, `release_writer`.
The field is left unclaimed while holding an actuation value no run
ever commanded — reachable non-maliciously as a maintenance write
left unrestored. The surviving peer then compares its staged image
against that foreign value and reports `diverged`, and every
`POST /promote` answers `not_converged`: two individually sound
fail-closed rules compose into a self-sealing dead end, because the
only actor that could rewrite the field is barred by the convergence
gate its own writes would satisfy.

Three facts about the current contract shape the run, and each is
asserted rather than assumed:

- The comparison's window. Decision 26's staged-versus-field verdict
  convicts only while the tracked line's source still stamps
  `source_owns_field: true`; the fenced owner's demotion is the very
  checkpoint that flips that stamp, and decision 87's `orphaned`
  verdict then supersedes `Diverged` outright. So the wedge's
  divergence is observable in exactly one place, and the run orders
  its scans into it: the survivor's comparison lands while its source
  still claims the field, and the survivor is not scanned again until
  the remedy's recovery ticks. That served verdict is what the gate is
  graded on.
- The demoted ex-owner's claim. Decision 97's fencing-loss reclaim is
  a recovery in its own right: the first scan the field stands
  unclaimed, the ex-owner's bound conditional `reclaim_writer` grant
  takes it back and walks it `promoting -> active`. A live demoted
  ex-owner therefore heals the pair by itself, and the unclaimed-field
  surface decision 94 records is observable only with that actor gone.
  The run removes it the way the recorded remedy presupposes — the
  operator stops the wedged field owner — and that removal is where
  decision 94's premise begins.
- The demoted ex-owner's own verdict. The customer pair's field owner
  is launched without a declared tracking source, but the standby
  announces its own monitor on every pull, so the demoted run adopts
  that successor and converges `orphaned` — decision 87's *promotable*
  verdict — the same shape the mutual-wired rig pair reports. A
  `Diverged` verdict therefore never stands on this pair long enough
  to be promoted against: the promote's own final-sync transfer pulls
  the demoted source's stamp and the `orphaned` adjudication
  supersedes the divergence. What closes the gate while the
  interposer's claim stands is decision 91's conditional orphan grant,
  refused by the field's own arbitration, and what closes it once the
  claim is released is the same grant granting — decision 94's own
  recorded boundary, where the ordinary promote is the lighter
  recovery. The leg refuses every promotion in the wedge with a named
  closed-gate verdict and never hands the field off, and posts none
  after the release so the recorded remedy is what it grades.

The run's phases, all driven so the episode needs no wall clock:

- converges the declared pair to `tracking` and gates the contract
  surface: the launched active's recorded owner token, the emitted
  model's carried boolean field output the perturbation lands on;
- induces the wedge through the run's dedicated plant-protocol
  client: the rogue `claim_writer` preempts the duty's standing claim
  and one field write lands off the staged value; the survivor's
  single comparison scan convicts it, then the duty's driven scans
  meet the fence and demote it in place under the `fenced` origin
  with its `field_claim_lost` journaled;
- asserts the gate: the survivor's served report reads `diverged`
  naming the skewed point with both sides' values, its journal carries
  exactly one `divergence_detected`, and `POST /promote` on both peers
  answers a named closed-gate `409` with no field hand-off — with the
  interposer's claim standing, decision 91's conditional orphan grant
  is the gate that refuses on this pair, and a `not_converged` answer
  would have to carry the diverged report;
- removes the wedged field owner, releases the rogue hold, and
  asserts the wedge's unclaimed surface on what now answers: the
  served report carries `field_claim: unclaimed`, a read-only mutation
  probe on the plant answers the field `unclaimed`, the un-commanded
  actuation still stands on the skewed point, and the survivor's
  verdict still closes the promote gate. No promote is posted here:
  where the demoted source's stamp already superseded the divergence
  with decision 87's promotable `orphaned` verdict, an ordinary promote
  would take the free field through decision 91's conditional orphan
  claim — decision 94's own recorded lighter recovery — and the leg
  grades the recorded remedy instead;
- runs the recorded remedy: the originally-active controller is
  relaunched as a fresh active on its declared listen and persistence
  files. Its conditional startup grant takes the free field, its first
  field-owning scans write the run's declared image over the
  un-commanded value, exactly one peer reports `active`, and the
  diverged survivor clears to `tracking` in place with exactly one
  journaled `divergence_resolved` carrying the compared points and
  both sides' values;
- restores the pair's launch roles — the relaunched owner is the
  launch role, the survivor is its tracking standby — and asserts the
  field stands the owner's declared image.

No promotion override exists and none is attempted: the leg's positive
claim is that restart-as-active is the recovery, and the `not_converged`
refusal is the contract declining the wrong one.

Usage:

    diverged_field_recovery.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `wedge-recovery-digest <sha256>` line prints — the check
runs two passes and compares them (`wedge-recovery-nondeterministic`).
A contract violation reports `diverged-field-recovery: …` lines on
stderr and exits 1 — the check's `wedge-recovery-failed`. The
`--tamper` cases doctor the leg's own expectations: `expect-promote`
wants the wedge's promote admitted — the override the contract refuses —
and `skip-relaunch` never relaunches the field owner, so the recorded
remedy never runs; each must fail naming the named evidence, the
check's `diverged-field-recovery-unchecked` cover.

A pinned release predating the contract, with a launched active holding no
recorded owner token or a relaunched controller holding no startup claim,
reports `wedge-recovery-digest inconclusive` rather than failing, until the
manifest repins a release carrying decision 90's served signal and
decision 94's remedy.
"""

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import divergence
import failover
import pair
import refusal
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored cases.
# The doctored cases: a leg asserting the wedge's promote was
# admitted — the override `not_converged` has no case for — and a leg
# that never relaunches the field owner — the restart-as-active
# recovery decision 94 records — must each surface the named
# diagnostic on the honest run rather than passing an unexercised
# contract.
LEG = {
    "order": 155,
    "title": "the diverged-field wedge-recovery leg",
    "passes": "wedge-recovery",
    "failed": "wedge-recovery-failed",
    "tampers": [
        {
            "name": "expect-promote",
            "passed": "an expect-promote case passed the diverged-field-recovery leg",
            "missed": "the expect-promote case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the wedge's promote admitted"
            ],
        },
        {
            "name": "skip-relaunch",
            "passed": "a skip-relaunch case passed the diverged-field-recovery leg",
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
    failure."""


# The interposer's claim token — a different owner than either peer
# pins, so the unconditional `claim_writer` preempts the duty's
# standing claim the finding's reproduction recorded. The value never
# reaches a digest (only the granted/refused verdict does), so it stays
# free to move without churning a recorded digest.
ROGUE_TOKEN = 0x4D57

WEDGE_TICKS = 3       # driven scans of the fenced field owner through
                       # the wedge — its first is the fenced write that
                       # demotes it in place, the second settles the
                       # transition onto `standby`
RECOVERY_TICKS = 10   # driven pair ticks spent on the reconvergence
RETURN_POLLS = 80     # the relaunched monitor's answer polls
POLL_SLEEP = 0.25     # cadence between those polls


def sync_kind(report):
    """The served StandbySync's variant name — `unsynchronized` and
    `degraded` are bare strings, the rest single-key objects. None for
    a settled `active`."""
    sync = report.get("sync") if isinstance(report, dict) else None
    if isinstance(sync, str):
        return sync
    if isinstance(sync, dict) and sync:
        return next(iter(sync))
    return None


def role_of(url, what, failures):
    """One served role report — the peer is serving, or the leg fails
    naming what it was reading."""
    report = pair.get(f"{url}/role", f"GET /role ({what})", failures)
    if report is None:
        failures.append(f"{what} served no role report")
        raise Abort
    return report


def try_role(url):
    """One best-effort served role report — None while a relaunched
    controller has not yet bound its listener."""
    try:
        return simulate.http(f"{url}/role")
    except Exception:
        return None


def role_changes(entries):
    """The `role_changed` payloads a journal entry list carries — the
    fenced demotion's `(from, to, origin)` walk."""
    return [
        entry["event"]["role_changed"]
        for entry in entries or []
        if "role_changed" in entry.get("event", {})
    ]


def journal_records(url, failures):
    """One peer's served journal entries."""
    entries = pair.get(f"{url}/journal", "GET /journal", failures)
    return entries if isinstance(entries, list) else []


def journal_counts(url, failures):
    """The served journal's divergence records — the detection and
    resolution `(tick, payload)` streams."""
    entries = journal_records(url, failures)
    return (
        divergence.journal_events(entries, "divergence_detected"),
        divergence.journal_events(entries, "divergence_resolved"),
    )


def rogue_claim(plant_io, failures):
    """The interposer's unconditional claim — the preempting take the
    finding's reproduction recorded. Returns the claim-state word."""
    verdict = plant_io.request({"op": "claim_writer", "owner": ROGUE_TOKEN})
    kind = failover.probe_kind(verdict)
    if kind != "granted":
        failures.append(
            f"the interposer's claim_writer answered {verdict} — the "
            "wedge the leg stages never stood"
        )
        raise Abort
    return kind


def rogue_write(plant_io, point, value, failures):
    """The interposer's single field write off the staged value — the
    un-commanded actuation the wedge leaves standing. Returns the
    value that landed."""
    write = plant_io.request({"op": "write", "point": point, "value": value})
    if write.get("result") != "done":
        failures.append(
            f"the interposer's write on point {point} answered {write}"
        )
        raise Abort
    landed = failover.field_read(plant_io, point, failures)["value"]
    if landed != value:
        failures.append(
            f"the interposer's write left point {point} at {landed}, "
            f"expected {value} — the wedge was never written"
        )
        raise Abort
    return landed


# The two named verdicts that close the promotion gate. `not_converged`
# is the convergence gate's own answer — the peer carries no proof the
# run it would resume is the one that has been running.
# `field_claim_failed` is the field's own arbitration answering instead:
# a peer whose verdict *is* promotable still cannot take a field whose
# standing claim names a live controller, which is exactly what the
# interposer's preempted claim is while it holds. Both are refusals
# with no hand-off, and the wedge stages both.
CLOSED_GATE = ("not_converged", "field_claim_failed")


def gate_verdict(refused):
    """The named gate verdict a promote refusal carries, or None when
    the answer is something the wedge never stages."""
    if not isinstance(refused, dict):
        return None
    return next((name for name in CLOSED_GATE if name in refused), None)


def refuse_promotes(peers, failures, want=None):
    """Every peer's `POST /promote` — the gate's answer, graded on the
    verdict that peer serves. `peers` is the `name -> monitor url` map
    the digest keys on: the served URLs carry ephemeral loopback ports,
    so a digest keyed on them would differ between two passes.
    `want` pins the verdict where the peer is known to carry no
    convergence evidence; otherwise either named closed-gate verdict is
    accepted, because which one answers depends on whether the peer's
    own verdict or the field's arbitration is the one standing in the
    way. Returns the per-peer `(status, refused, verdict)` triples."""
    refusals = {}
    for name, url in peers.items():
        status, refused = pair.request(f"{url}/promote", {})
        verdict = gate_verdict(refused)
        refusals[name] = (status, refused, verdict)
        if status != 409 or verdict is None or (
                want is not None and verdict != want):
            failures.append(
                f"POST /promote on {name} answered {status} {refused} — "
                "the wedge's gate must refuse with a named "
                f"{want or 'closed-gate'} verdict, never admit"
            )
    return refusals


def refusal_sync(refused):
    """The sync report a `not_converged` refusal carries."""
    if isinstance(refused, dict) and isinstance(
        refused.get("not_converged"), dict
    ):
        return refused["not_converged"].get("sync")
    return None


def await_resolved(owner_url, tracked_url, failures):
    """Drive tracking-first pair ticks until the survivor's served
    verdict returns to `tracking`. Returns `(report, ticks)`."""
    report = None
    for index in range(RECOVERY_TICKS):
        pair.scan(tracked_url, failures)
        pair.scan(owner_url, failures)
        report = role_of(tracked_url, "the surviving peer", failures)
        if report.get("role") == "standby" and sync_kind(report) == "tracking":
            return report, index + 1
    return report, RECOVERY_TICKS


def await_image(owner_url, tracked_url, plant_io, point, failures):
    """Drive tracking-first pair ticks until the field stores the
    relaunched owner's *declared image* for `point` — the first
    field-owning writes are what overwrite the un-commanded value.
    The comparison is against the image, not the pre-wedge value: a
    carried output is dynamic, so the owner's declared image
    legitimately moves once the plant steps again, and only the
    field agreeing with the image that wrote it is the wedge's
    evidence. Returns the ticks it took and the value that landed,
    None when the field never agreed."""
    landed = None
    for index in range(RECOVERY_TICKS):
        pair.scan(tracked_url, failures)
        snapshot = pair.scan(owner_url, failures)
        landed = refusal.field_out_samples(plant_io).get(
            point, {}).get("value")
        if landed == simulate.snapshot_point(snapshot, point):
            return index + 1, landed
    return RECOVERY_TICKS, None


def relaunch_active(args, rig, duty_listen):
    """Relaunch the wedged field owner as its configured active on its
    declared listen and persistence files — decision 94's recorded
    remedy. Returns `(url, preamble)`; url None where the relaunch
    never bound a listener."""
    process, url, preamble = pair.spawn_peer(
        args.controller,
        args.model,
        args.dt,
        rig.plant_addr,
        None,
        rig.duty_files,
        listen=duty_listen,
        pair_token=pair.PAIR_TOKEN,
    )
    rig.duty = process
    rig.duty_url = url
    return url, preamble


def wedge_recovery_run(args, declared, point, tamper, failures):
    """The wedge and its recorded remedy on the consumer pair. Returns
    `(digest_entries, evidence)`."""
    digest_entries, evidence = [], {}
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io

        # --- Phase 1 — the settled pair and the contract surface -----
        converged = rig.converge(failures)
        owner = converged["owner"]
        staged = simulate.snapshot_point(owner, point)
        if not isinstance(staged, dict) or "bool" not in staged:
            failures.append(
                f"the carried point {point} serves {staged} in the "
                "owner's image — the wedge perturbs a boolean field "
                "output"
            )
            raise Abort
        if failover.owner_token(rig.duty_preamble) is None:
            raise Inconclusive(
                "the launched active's preamble holds no field-claim "
                "token — the pinned release predates the recorded "
                "field ownership the wedge preempts"
            )
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "point": point,
                "staged": staged,
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # --- Phase 2 — the wedge: preempt and one field write -------
        # The scan order is the wedge's own reachability, and it is
        # load-bearing: the survivor's same-tick comparison convicts
        # only while the tracked line's source still stamps
        # `source_owns_field: true`, and the fenced owner's demotion
        # is the checkpoint that flips that stamp. So the survivor
        # scans first — one scan, the one the converge left poised to
        # compare — and only then does the owner's fenced write land
        # and demote it. The survivor is not scanned again until the
        # remedy's recovery ticks, so its served verdict is the one
        # the comparison convicted.
        injected = {"bool": not staged["bool"]}
        claim = rogue_claim(plant_io, failures)
        landed = rogue_write(plant_io, point, injected, failures)
        pair.scan(standby_url, failures)
        report = role_of(standby_url, "the surviving peer", failures)
        want = [{"point": point, "staged": staged, "field": injected}]
        convicted = (
            report.get("role") == "standby"
            and divergence.diverged_mismatches(report) == want
        )
        for _ in range(WEDGE_TICKS):
            pair.scan(duty_url, failures)
        demoted = role_of(duty_url, "the fenced field owner", failures)
        if demoted.get("role") not in ("demoting", "standby"):
            failures.append(
                f"the preempted field owner reports {demoted} — its "
                "first fenced write never demoted it in place"
            )
        duty_journal = journal_records(duty_url, failures)
        losses = divergence.journal_events(duty_journal, "field_claim_lost")
        walk = role_changes(duty_journal)
        fenced_walk = any(
            change.get("to") in ("demoting", "standby")
            and change.get("origin") == "fenced"
            for change in walk
        )
        if not losses:
            failures.append(
                "the preempted field owner's journal carries no "
                "field_claim_lost — the fencing loss left no record"
            )
        if not fenced_walk:
            failures.append(
                f"the preempted field owner's journal carries the role "
                f"walk {walk} — no fenced-origin demotion was journaled"
            )
        digest_entries.append(
            {
                "phase": "wedge",
                "claim": claim,
                "injected": injected,
                "landed": landed,
                "convicted": convicted,
                "demoted": demoted.get("role"),
                "demoted_sync": sync_kind(demoted),
                "losses": len(losses),
                "fenced_walk": fenced_walk,
            }
        )

        # --- Phase 3 — the gate: the diverged verdict and refusals --
        if not convicted:
            failures.append(
                "the surviving peer never reported the staged-versus-"
                f"field divergence — GET /role answers {report}; "
                f"expected the diverged report naming point {point} "
                f"with staged {staged} against field {injected}"
            )
        detections, resolutions = journal_counts(standby_url, failures)
        if len(detections) != 1:
            failures.append(
                f"the surviving peer's journal carries "
                f"{len(detections)} divergence_detected records "
                f"{detections} — the wedge convicts once"
            )
        if resolutions:
            failures.append(
                f"the surviving peer's journal carries divergence "
                f"resolutions {resolutions} during the wedge — the "
                "gate must stay closed until positive evidence"
            )
        if tamper == "expect-promote":
            # The doctored expectation: the wedge's promote admitted.
            refuse_promotes({rig.duty_decl["name"]: duty_url,
                             rig.standby_decl["name"]: standby_url},
                            failures)
            digest_entries.append(
                {"phase": "gate", "convicted": convicted}
            )
            raise Abort(
                "the doctored expectation wanted the wedge's promote "
                "admitted and the contract refused it"
            )
        # Both peers are refused and neither hands the field off. Which
        # named gate answers depends on the verdict each peer serves:
        # the demoted field owner adopts its successor's announced
        # monitor and converges `orphaned` — decision 87's promotable
        # verdict — so while the interposer's controller claim stands,
        # decision 91's conditional orphan grant is the gate that
        # refuses, and the refusal names the field's own arbitration.
        owner_name = rig.duty_decl["name"]
        survivor_name = rig.standby_decl["name"]
        refusals = refuse_promotes({owner_name: duty_url}, failures)
        gate = refuse_promotes({survivor_name: standby_url}, failures)
        for _name, (_status, _refused, verdict) in sorted(gate.items()):
            carried = refusal_sync(gate[_name][1])
            if verdict == "not_converged" \
                    and divergence.diverged_mismatches(
                        {"sync": carried}) != want:
                failures.append(
                    f"the surviving peer's refusal carries "
                    f"{carried} — a not_converged answer "
                    f"must carry the diverged report naming point "
                    f"{point}"
                )
        evidence["refusals"] = sorted(
            {verdict for _status, _refused, verdict
             in {**refusals, **gate}.values()})
        digest_entries.append(
            {
                "phase": "gate",
                "convicted": convicted,
                "mismatches": divergence.diverged_mismatches(report),
                "detections": len(detections),
                "refusals": {
                    # The manifest's peer names, never the served URLs:
                    # those carry ephemeral loopback ports and would
                    # churn the digest between two passes.
                    name: [status, verdict]
                    for name, (status, _refused, verdict) in
                    sorted({**refusals, **gate}.items())
                },
            }
        )
        if failures:
            raise Abort

        # --- Phase 4 — the unclaimed-field surface ------------------
        # The operator removes the wedged field owner — decision 94's
        # recorded remedy is exactly a fresh authority supplying what
        # the gated peers cannot, and decision 97's fencing-loss
        # reclaim means a live demoted ex-owner would retake a free
        # field on its next scan and close the window the unclaimed
        # signal is read in. The stop is the remedy's first half; the
        # relaunch below is its second.
        pair.stop(rig.duty)
        plant_io.request({"op": "release_writer"})
        # One driven scan so the survivor's own claim probe reads the
        # released field: the served `field_claim` is that probe's
        # answer, and it has not looked since the interposer's hold
        # stood. Its pull now misses — the tracked source is gone — so
        # the verdict it serves degrades; the field claim and the
        # gate's closure are what this phase reads.
        pair.scan(standby_url, failures)
        served = role_of(standby_url, "the surviving peer", failures)
        probe = plant_io.request(
            {"op": "write", "point": point, "value": injected}
        )
        field_kind = failover.probe_kind(probe)
        uncommanded = failover.field_read(
            plant_io, point, failures
        )["value"]
        if served.get("field_claim") != "unclaimed":
            failures.append(
                "GET /role reports field_claim "
                f"{served.get('field_claim')!r} on the wedged field — "
                "the served unclaimed-field signal never landed"
            )
        if field_kind != "unclaimed":
            failures.append(
                f"the wedged field's mutation probe answered "
                f"{field_kind} — the field must refuse every mutation "
                "while no writer stands"
            )
        if uncommanded != injected:
            failures.append(
                f"the wedged field stores {uncommanded} on point "
                f"{point} — the un-commanded actuation #730 named must "
                f"still stand at {injected}"
            )
        if sync_kind(served) not in ("diverged", "orphaned", "degraded"):
            failures.append(
                f"the surviving peer reports {served} on the released "
                "field — the wedge's verdict must still be a named "
                "un-converged one while the field stands un-commanded"
            )
        evidence["unclaimed"] = served.get("field_claim")
        digest_entries.append(
            {
                "phase": "unclaimed",
                "served_claim": served.get("field_claim"),
                "field": field_kind,
                "uncommanded": uncommanded,
                "diverged": sync_kind(served),
            }
        )
        if failures:
            raise Abort

        # --- Phase 5 — the recorded remedy: restart-as-active ------
        duty_listen = duty_url.removeprefix("http://")
        if tamper != "skip-relaunch":
            duty_url, preamble = relaunch_active(args, rig, duty_listen)
            if duty_url is None:
                failures.append(
                    "the relaunched controller exited at startup — its "
                    f"preamble reads: "
                    f"{'; '.join(preamble) or 'no diagnostic'}"
                )
                raise Abort
            if failover.owner_token(preamble) is None:
                raise Inconclusive(
                    "the relaunched controller held no startup claim — "
                    "the pinned release predates the conditional "
                    "startup grant the remedy takes the free field with"
                )
        owner_report = None
        for _ in range(RETURN_POLLS):
            owner_report = try_role(duty_url)
            if owner_report is not None:
                break
            time.sleep(POLL_SLEEP)
        if owner_report is None:
            failures.append(
                "the relaunched field owner never served its monitor — "
                "the restart-as-active recovery did not run"
            )
            raise Abort
        if owner_report.get("role") != "active":
            failures.append(
                "the relaunched controller did not restart active — "
                f"GET /role answers {owner_report}"
            )
            raise Abort
        evidence["relaunched"] = owner_report.get("role")
        digest_entries.append(
            {"phase": "relaunch", "role": owner_report.get("role")}
        )

        # --- Phase 6 — the recovery: the declared image lands ------
        # The grant is taken at activation, before the relaunched run
        # has scanned anything, so the served `field_claim` is its own
        # claim probe's answer and only reports once the first
        # field-owning scan has run — the same scans that write the
        # declared image over the un-commanded value.
        resolved, ticks = await_resolved(duty_url, standby_url, failures)
        image_ticks, landed = await_image(duty_url, standby_url,
                                          plant_io, point, failures)
        resolved = role_of(standby_url, "the surviving peer", failures)
        healed_owner = role_of(duty_url, "the relaunched owner", failures)
        if landed is None:
            failures.append(
                f"the relaunched owner's field-owning writes never left "
                f"point {point} holding its own declared image — the "
                "un-commanded actuation the wedge left standing was "
                "never overwritten"
            )
        if healed_owner.get("field_claim") != "held":
            failures.append(
                "the relaunched controller reports field_claim "
                f"{healed_owner.get('field_claim')!r} — the conditional "
                "startup grant must take the free field"
            )
        if sync_kind(resolved) != "tracking":
            failures.append(
                "the diverged survivor never cleared to tracking — the "
                f"restart-as-active remedy did not heal the pair: "
                f"GET /role answers {resolved}"
            )
            raise Abort
        # The resolution record: a standing `Diverged` verdict
        # resolves exactly once, carrying the compared points. Where
        # the demoted source's stamp already superseded it with the
        # `orphaned` verdict, no resolution record is owed — the
        # survivor reconverges through the ordinary pull — so the leg
        # reads zero as the honest count and one as the audited one,
        # and refuses anything above one.
        resolutions = journal_counts(standby_url, failures)[1] or []
        if len(resolutions) > 1:
            failures.append(
                f"the survivor's journal carries {len(resolutions)} "
                f"divergence_resolved records {resolutions} — the "
                "recovery resolves the wedge at most once"
            )
        elif resolutions and not all(
            entry.get("staged") == entry.get("field")
            and isinstance(entry.get("point"), int)
            for entry in (resolutions[0][1].get("compared") or [])
        ):
            failures.append(
                f"the divergence_resolved record's compared evidence "
                f"{resolutions[0][1]} — it must carry the compared "
                "points with both sides' values"
            )
        evidence["resolved"] = (resolutions[0][0] if resolutions
                               else healed_owner.get("tick"))
        digest_entries.append(
            {
                "phase": "recovered",
                "ticks": ticks,
                "image_ticks": image_ticks,
                "landed": landed,
                "sync": sync_kind(resolved),
                "claim": healed_owner.get("field_claim"),
                "resolutions": len(resolutions),
                "compared_point": point,
            }
        )

        # --- Phase 7 — the restored pair ----------------------------
        healed = rig.converge(failures)
        if healed["duty_role"].get("role") != "active":
            failures.append(
                "the restored field owner does not report active — "
                f"GET /role answers {healed['duty_role']}"
            )
        if healed["standby_role"].get("role") != "standby" \
                or sync_kind(healed["standby_role"]) != "tracking":
            failures.append(
                "the restored pair's tracker is not a tracking standby "
                "— the launch roles did not restore: "
                f"GET /role answers {healed['standby_role']}"
            )
        samples = refusal.field_out_samples(plant_io)
        for mismatch in refusal.field_mismatches(healed["owner"], samples):
            failures.append(
                f"{mismatch} after the recovery — the field does not "
                "hold the relaunched owner's declared image"
            )
        # The field holds the relaunched owner's declared image — the
        # whole out-image the field-owning writes keep producing, which
        # `field_mismatches` just checked. The skewed point's own value
        # is recorded, not asserted: a carried output is dynamic, so
        # the run's image legitimately moves once the plant steps
        # again, and only the field agreeing with the image that wrote
        # it is the wedge's evidence (`await_image` above).
        field_now = samples.get(point, {}).get("value")
        evidence["restored"] = healed["owner"].get("tick")
        digest_entries.append(
            {
                "phase": "restored",
                "ticks": healed["ticks"],
                "duty_role": healed["duty_role"],
                "standby_role": healed["standby_role"],
                "field": field_now,
            }
        )
    finally:
        if rig is not None:
            rig.close()
    return digest_entries, evidence


def wedge_recovery_pass(args, tamper):
    """The wedge-recovery run. Returns `(digest_entries, evidence,
    failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "diverged-field-recovery leg has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    point = divergence.carried_point(model)
    if point is None:
        raise Abort(
            f"the emitted model declares no carried boolean field "
            f"output bound to {divergence.CARRIED_SIGNAL} — the "
            "diverged-field-recovery leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {"point": point}, []
    try:
        entries, run_evidence = wedge_recovery_run(
            args, declared, point, tamper, failures
        )
        digest_entries.extend(entries)
        evidence.update(run_evidence)
    except Abort as abort:
        failures.extend(str(item) for item in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
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
        choices=["expect-promote", "skip-relaunch"],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = wedge_recovery_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "diverged-field-recovery: an inconclusive run under "
                f"the {args.tamper} doctor offers the doctored case "
                "no evidence"
            )
            return 1
        eprint(f"diverged-field-recovery: inconclusive — {inconclusive}")
        print(f"wedge-recovery-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"diverged-field-recovery: {line}")
        return 1
    for failure in failures:
        eprint(f"diverged-field-recovery: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"diverged-field-recovery: the {args.tamper} case "
                "passed silently — the leg never noticed the doctored "
                "recovery"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"wedge-recovery-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the wedge left point "
        f"{evidence['point']} un-commanded with every promotion refused "
        + " and ".join(evidence["refusals"]) + ", the "
        f"released field reported {evidence['unclaimed']}, the "
        f"relaunched owner reported {evidence['relaunched']}, the "
        f"survivor resolved at tick {evidence['resolved']} and the pair "
        f"reconverged by tick {evidence['restored']} with the launch "
        "roles restored"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())