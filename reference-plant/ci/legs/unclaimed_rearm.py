#!/usr/bin/env python3
"""The unclaimed-rearm leg for the reference plant — the
consumer-boundary mirror of the qa rig's unclaimed-field re-arm
scenario (scenario 3700, pinned in-workspace on the settled #621
behavior the qax-20260918-008 run verified), never before run on the
customer-owned deployment: a field write refused `unclaimed` while
no claim stands is a recoverable ownerless window, not supersession
— the recorded owner re-arms inline through one conditional,
non-preempting `ensure_writer` and the write lands without
`field_claim_lost` or demotion (WW-ENG-003, WW-LCM-001).

The claim-reclaim leg (`ci/legs/claim_reclaim.py`) exercises the
other half of the claim lifecycle: a foreign attachment preempts
the field and holds it, so the owner's next write is refused under
another standing claim and the fenced write takes the owner down
before the released claim is ever re-armed. Nothing in the pair
stage stages the window *behind* a live owner — a claim opened and
handed back in the same breath, so the field stands unclaimed
while the owner's own connection never dropped — and that is the
window the contract answers: an owner whose connection is alive
lost nothing, so it re-arms in place through one conditional grant
that cannot preempt, instead of journal a fencing loss and demote
the redundant pair's field owner over a claim nobody holds.

This leg runs the episode on the manifest-declared pair, ordered by
the driven harness — a peer applies live only inside its own
`POST /scan`, so the ownerless window is the harness's to hold open:

- converges the declared pair in driven mode on the released images
  and gates the contract surface: the launched active's recorded
  owner token, the standing claim fencing third-party mutations
  while naming that token, the manifest's declared durable journal
  files, and the served `io_health` ledger the fencing-loss half of
  the audit reads;
- opens the ownerless window with the rig's own raw plant client —
  `claim_writer` preempts the standing claim unconditionally and
  `release_writer` empties the holder set in the same breath, the
  field returning to `unclaimed` behind the owner's live
  connection. The claim ops ride the raw client because
  `claim_writer` is no `dcs-plant-ctl` op and the tool's own
  conditional claim could never open this window: it is granted
  only where the field already stands unclaimed or already names its
  own token, never over the live owner's hold;
- watches the recorded owner's next scans across the window — only
  the owner is driven, so every field movement in the window is its
  own: its writes land (the watched field output carries the owner's
  own image) and its plant tick advances (a `step` is a field
  mutation too, and the re-armed claim has to carry it), the owner
  reports `active` throughout, the tracking peer stays converged
  `standby`, the owner's `io_health.failed_writes` fencing-loss
  ledger never moves, and third-party mutation probes keep meeting
  the fail-closed field — `unclaimed` while the window still stands,
  `fenced` once the re-arm has landed;
- audits the journal floors taken before the induction: neither
  peer's served journal nor either declared durable journal file
  gains a `field_claim_lost` or a `role_changed` record across the
  window — the re-arm is inline and leaves no trace of an
  episode;
- proves the re-armed claim is real, not an open field: a
  third-party probe is fenced naming the recorded owner's token
  again, a foreign attachment's conditional `ensure_writer` is
  refused, its same-value write probe is fenced at the point, and
  the owner's writes keep landing after those probes — the re-armed
  claim fences for its own owner rather than against it;
- then stages the contrast the contract discriminates, on the same
  settled pair: the foreign attachment claims again and *holds*.
  That refusal is under another standing claim, so the owner's
  write is fenced, the owner demotes in place through its own
  fenced write, its served journal gains the `field_claim_lost`
  attributed to the induction token beside the `active → demoting
  → standby` fenced walk, and the field carries no foreign write.
  Releasing the held claim hands the field back to the
  loss-marked ex-owner's bound reclaim, which walks it
  `standby → promoting → active` with its own token — the two
  refusals, two outcomes, staged on one pair;
- settles the pair back to its launch roles through tracking-first
  ticks — identical images, the field owner `active`, the declared
  standby `tracking` it — with the claim under the launch owner's
  own token, so the legs behind this one start where it found them.

The contract lands with the v0.3.0 release line; an artifact set cut
before it reads this way: where the launched tooling predates it — no
recorded owner token at launch, a claim verb answered
`invalid_request`, a fencing verdict naming no standing owner, the
declared pair carrying no durable journal files, a served snapshot
carrying no `io_health` ledger, or the harness's own post-release
probe answering the window's `unclaimed` verdict as a plain fencing
refusal — the run's evidence is the pre-contract shape and the leg
reports `unclaimed-rearm-digest inconclusive` rather than asserting,
until the manifest repins a release carrying the contract.

Usage:

    unclaimed_rearm.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `unclaimed-rearm-digest <sha256>` line prints — the
check runs two passes and compares them
(`unclaimed-rearm-nondeterministic`). A contract violation reports
`unclaimed-rearm: …` lines on stderr and exits 1 — the check's
`unclaimed-rearm-failed`. `--tamper expect-foreign` doctors the
leg's own re-arm expectation to the defect shape — asserting the
re-armed claim came to rest under the foreign claimant rather than
the recorded owner — so the leg proves its re-arm assertion fires
rather than passing an unexercised contract.
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
import failover
import pair
import simulate
import stranded_rejoin


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored case. The
# doctored case: a leg asserting the re-armed claim came to rest
# under the foreign claimant must surface the named diagnostic on the
# honest standing-owner re-arm — never a silently unexercised pass.
LEG = {
    # The next free slot after the release-line claim legs and the two
    # legs origin/main added beside them (claim-skew-bound 730,
    # demote-release-stays-released 750, reclaim-convergence-gate
    # 770, pending-serving-bound 780, shared-state-file-refusal 790,
    # usurped-claim-reclaim 800, attributed-switch-isolation 810) —
    # the stage runs the legs in this order and no two may share one.
    # Two legs that claim a slot independently while their branches
    # diverge resolve the way every merge does: the leg already on
    # main keeps its slot and the one arriving from a branch takes the
    # next.
    "order": 820,
    "title": "the unclaimed-rearm leg",
    "passes": "unclaimed-rearm",
    "tampers": [
        {
            "name": "expect-foreign",
            "passed": "a doctored foreign-owner expectation passed the unclaimed-rearm leg",
            "missed": "the expect-foreign case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the claim to rest under the foreign claimant",
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
    product failure. The first arg is the stable reason the
    `inconclusive` digest line prints — two identical passes must
    share it — and the optional second arg the run's own evidence,
    reported on stderr only, where minted owner tokens and ephemeral
    verdicts belong."""


# The driven-scan bounds the episode's phases run: the owner's
# re-arm lands inside its first scan after the window opens (the watch
# spans a handful of driven scans so a landed re-arm is watched across
# a few settled cycles), the fenced demote-in-place settles inside a
# couple of scans, the loss-marked ex-owner's bound reclaim walks back
# to the field inside its budget, and the settle train proves the
# reconverged pair identical before its launch roles are read.
REARM_SCANS = 4
DEMOTE_SCANS = 4
RECLAIM_SCANS = 8
SETTLE_TICKS = 4

# The staging attachment's foreign owner token — a small fixed value
# that cannot collide with a controller's per-process minted token,
# distinct from the tokens the other claim legs stage.
FOREIGN_OWNER = 0xF1A7

# The doctored expectation's stable evidence prefix — every failure
# the tampered pass records carries it, so the check's negative case
# finds it whether the honest run re-armed to the recorded owner or a
# predating release offered the doctored case no evidence at all.
TAMPER_EVIDENCE = (
    "the doctored expectation wanted the claim to rest under the "
    "foreign claimant"
)


def journal_entries(path):
    """A `--journal-file`'s entry records in file order — the durable
    half of the window's audit. The stranded-rejoin leg's own
    projection, reused rather than copied."""
    return stranded_rejoin.journal_entries(path)


def write_fenced(verdict):
    """Whether a write probe's answer is the point-level fencing
    refusal — `write` carries the point's `io`-kind `fenced` error,
    where the plant-level `step` probe carries the named `fenced` kind
    with no point-level counterpart."""
    error = (verdict or {}).get("error") or {}
    inner = error.get("error")
    return (
        error.get("kind") == "io"
        and isinstance(inner, dict)
        and "fenced" in inner
    )


def owner_word(token, owner_token):
    """The claim-identity word a fencing verdict's owner reads as in
    the digest — the recorded owner, the leg's foreign claimant,
    nobody, or another — never the raw per-process value two
    identical passes cannot share."""
    if token == owner_token:
        return "owner"
    if token == FOREIGN_OWNER:
        return "foreign"
    if token is None:
        return "unowned"
    return "other"


def ledger(snapshot):
    """The snapshot's fencing-loss ledger — `io_health`'s count of
    failed output writes, the record the re-arm's inline recovery must
    leave unmoved. None when the served snapshot carries no
    `io_health` section at all: a release predating the section, not a
    run that lost a write."""
    health = (snapshot or {}).get("io_health")
    return None if health is None else health.get("failed_writes")


def fail_closed(kind):
    """Whether a third-party mutation probe's claim-state word is the
    field's fail-closed shape — `unclaimed` while the ownerless window
    still stands, `fenced` once a claim does. Only a granted answer is
    the defect: an attachment mutating the field with no claim behind
    it."""
    return kind in ("unclaimed", "fenced")


def try_role(url):
    """`GET /role` that answers None on transport error — the
    best-effort restore, where a dead monitor is data, not a harness
    failure."""
    return claim_reclaim.try_role(url)


def try_scan(url):
    """One driven `POST /scan` that answers None on transport error —
    the restore path only; the pass's own phases use `pair.scan` so
    transport errors are recorded failures."""
    return claim_reclaim.try_scan(url)


def foreign_write_probe(plant_io, point, failures):
    """A foreign attachment's same-value write probe — the mutation a
    re-armed claim must refuse at the point. The same value makes a
    granted probe idempotent, so the defect shape leaves no field
    damage behind it."""
    current = failover.field_read(plant_io, point, failures)["value"]
    return plant_io.request(
        {"op": "write", "point": point, "value": current}
    )


def field_probe_point(model):
    """The point the foreign attachment's write probe targets: the
    lowest-id channel-backed field input the emitted model declares,
    the plant-side bound point a field-tool attachment can write
    under the field's claim. A same-value write makes a granted probe
    idempotent. None when the model declares no channel-backed field
    input — an internal point carries no channel and no plant-side
    binding, so the plant server serves no such point at all."""
    channeled = [
        point
        for point in model.get("io_points", [])
        if point.get("channel")
    ]
    inputs = [point for point in channeled if point.get("direction") == "in"]
    points = inputs or channeled
    return min(point["id"] for point in points) if points else None


def gate_contract_surface(rig, verdict_io, owner_token, failures,
                          evidence):
    """The contract surface the episode asserts on: the standing
    claim fencing third-party mutations while naming the launch
    owner, and the durable journal files the manifest declares. Each
    absence is the release predating the substrate, never a violation
    of it."""
    if rig.duty_files.get("journal_file") is None or (
        rig.standby_files.get("journal_file") is None
    ):
        raise Inconclusive(
            "the manifest's pair declares no journal files — the "
            "durable half of the window's audit is absent"
        )
    probe0 = verdict_io.request({"op": "step", "dt": 0})
    evidence["probe0"] = probe0
    if not claim_reclaim.mutation_fenced(probe0):
        failures.append(
            "the field held no writer claim after convergence — a "
            f"third-party probe answered {probe0}, so the leg has no "
            "standing claim to open the ownerless window behind"
        )
        raise Abort
    if claim_reclaim.verdict_owner(probe0) is None:
        raise Inconclusive(
            "the fencing verdict names no standing owner — the pinned "
            "release predates the verdict attribution the re-arm's "
            "owners are read against",
            f"the standing fencing verdict was {probe0}",
        )
    if claim_reclaim.verdict_owner(probe0) != owner_token:
        failures.append(
            "the standing claim names a foreign token, not the launch "
            f"owner — the pair is not in its launch claim state: "
            f"{probe0}"
        )
        raise Abort


def rearm_watch(plant_io, verdict_io, owner_url, tracker_url, cmd,
                losses0, failures, evidence):
    """The window watch: the driven owner scans that must re-arm the
    claim inline behind their owner's live connection. Each round
    reads the owner's role and image, the tracking peer's role, the
    owner's fencing-loss ledger, the watched field output, the plant's
    own tick, and a third-party mutation probe. Returns the per-round
    observations; raises `Inconclusive` where a served snapshot
    carries no `io_health` ledger to read."""
    # The plant's own tick before the window's first scan: only the
    # owner is driven here, so a tick that does not advance is a field
    # mutation the owner could not make — its `step` refused under a
    # claim the re-arm never took back.
    baseline = failover.field_read(plant_io, cmd, failures).get("tick")
    watch = []
    previous = baseline
    for number in range(REARM_SCANS):
        owner = pair.scan(owner_url, failures)
        report = pair.get(f"{owner_url}/role", "GET /role", failures)
        partner = pair.get(
            f"{tracker_url}/role", "GET /role", failures
        )
        failed_writes = ledger(owner)
        if failed_writes is None:
            raise Inconclusive(
                "the served snapshot carries no io_health section — "
                "the pinned release predates the fencing-loss ledger "
                "the window's audit reads",
                f"the served snapshot carried {sorted(owner)}",
            )
        sample = failover.field_read(plant_io, cmd, failures)
        probe = verdict_io.request({"op": "step", "dt": 0})
        kind = failover.probe_kind(probe)
        image = simulate.snapshot_point(owner, cmd)
        watch.append(
            {
                "round": number,
                "owner": report.get("role"),
                "peer": partner.get("role"),
                "peer_sync": stranded_rejoin.sync_kind(partner),
                "plant_tick": sample.get("tick"),
                "field": "landed" if sample.get("value") == image
                else "stalled",
                "ledger": "empty" if failed_writes == losses0
                else "grew",
                "probe": kind,
            }
        )
        if report.get("role") != "active":
            failures.append(
                "the field owner left active across the ownerless "
                f"window — the unclaimed refusal demoted it instead "
                f"of re-arming inline: {report}"
            )
            raise Abort
        if not claim_reclaim.converged_sync(partner):
            failures.append(
                "the tracking peer left converged standby across the "
                f"ownerless window: {partner}"
            )
            raise Abort
        if sample.get("value") != image:
            failures.append(
                "the owner's write never landed across the ownerless "
                f"window — the field carries {sample.get('value')} "
                f"while its image reports {image}: the inline re-arm "
                "dropped the write or never ran"
            )
            raise Abort
        if not isinstance(sample.get("tick"), int) or (
            isinstance(previous, int)
            and sample["tick"] <= previous
        ):
            failures.append(
                "the owner's scan stopped stepping the plant across "
                f"the ownerless window — the plant tick reads "
                f"{sample.get('tick')} over {previous}: the re-armed "
                "claim never carried the scan's own field mutation"
            )
            raise Abort
        previous = sample["tick"]
        if failed_writes != losses0:
            failures.append(
                "the owner's fencing-loss ledger grew across the "
                f"ownerless window — io_health.failed_writes reads "
                f"{failed_writes} over the baseline {losses0}: the "
                "refused write was counted as a lost one"
            )
            raise Abort
        if not fail_closed(kind):
            failures.append(
                "the field accepted a third-party mutation with no "
                f"claim behind it across the window: {probe}"
            )
            raise Abort
    evidence["watch"] = watch
    return watch


def unclaimed_rearm_pass(args, tamper):
    """The unclaimed-rearm run: converge and gate, open the ownerless
    window with a same-breath preempt-and-release, watch the recorded
    owner's inline re-arm, audit the journal floors, prove the
    re-armed claim real against foreign probes, stage the contrast —
    a held foreign claim still fencing and demoting — settle the
    field back through the loss-marked ex-owner's reclaim, and
    reconverge the pair to its launch roles. Returns `(digest_entries,
    evidence, failures)`; raises `Inconclusive` where the pinned
    release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the unclaimed-"
            "rearm leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = verdict_io = induct_io = foreign_io = None
    owner_url = tracker_url = None
    held = False
    try:
        rig = pair.launch_pair(args, declared)
        owner_url, tracker_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io
        # The claim attachments the episode stages through: the rig's
        # own client stays read-only — `verdict_io` runs the
        # third-party mutation probes and never holds a claim,
        # `induct_io` opens and later closes the ownerless window
        # through the same preempt-and-release, and `foreign_io` runs
        # the different-owner conditional probe the re-armed claim
        # must refuse.
        verdict_io = simulate.PlantClient(rig.plant_addr)
        induct_io = simulate.PlantClient(rig.plant_addr)
        foreign_io = simulate.PlantClient(rig.plant_addr)
        with open(args.model) as handle:
            model = json.load(handle)
        points = failover.signal_points(model)
        if points is None:
            raise Abort(
                "the emitted model declares no p101-cmd/level-primary "
                "signal points — the leg has no field output to watch"
            )
        cmd = points["cmd"]
        probe_point = field_probe_point(model)
        if probe_point is None:
            raise Abort(
                "the emitted model declares no channel-backed field "
                "input — the leg has no point for its foreign write "
                "probe"
            )

        # Phase 1 — convergence on the released images and the
        # contract surface: the launched active's recorded claim
        # token, the fencing-loss ledger the window's audit reads, and
        # the substrate the episode's own evidence depends on. A
        # release that claims only on promotion predates the seam the
        # inline re-arm runs on.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        owner_token = failover.owner_token(rig.duty_preamble)
        if owner_token is None:
            raise Inconclusive(
                "the launched active recorded no claim line — the "
                "pinned release claims only on promotion, predating "
                "the claim lifecycle the unclaimed re-arm rides on"
            )
        losses0 = ledger(converged["owner"])
        if losses0 is None:
            raise Inconclusive(
                "the served snapshot carries no io_health section — "
                "the pinned release predates the fencing-loss ledger "
                "the window's audit reads"
            )
        gate_contract_surface(
            rig, verdict_io, owner_token, failures, evidence
        )
        held0 = failover.field_read(plant_io, cmd, failures)["value"]
        if held0 != simulate.snapshot_point(converged["owner"], cmd):
            failures.append(
                f"the field carries {held0} on the served out-point "
                "while the field owner reports "
                f"{simulate.snapshot_point(converged['owner'], cmd)} — "
                "the settled pair's writes never landed"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"].get("role"),
                "standby_role": converged["standby_role"].get("role"),
                "probe": "fenced",
                "owner": "named",
                "ledger": "present",
                "field": held0,
            }
        )

        # The floors the window's journal audit reads above: each
        # peer's served journal length and durable journal file entry
        # count, taken while the claim stands under the launch owner.
        floors = {
            "owner": len(
                pair.get(f"{owner_url}/journal", "GET /journal", failures)
            ),
            "tracker": len(
                pair.get(f"{tracker_url}/journal", "GET /journal", failures)
            ),
            "owner_file": len(journal_entries(rig.duty_files["journal_file"])),
            "tracker_file": len(
                journal_entries(rig.standby_files["journal_file"])
            ),
        }

        # Phase 2 — the induction: `claim_writer` preempts the
        # standing claim unconditionally and `release_writer` empties
        # the holder set in the same breath, so the field stands
        # unclaimed behind the owner's still-live connection. The
        # window is read from the released attachment itself — `unclaimed`
        # while it stands, `fenced` once the owner's re-arm has landed
        # — both the fail-closed field; only a mutation answer is the
        # defect.
        claim = induct_io.request(
            {"op": "claim_writer", "owner": FOREIGN_OWNER}
        )
        evidence["claim"] = claim
        if claim_reclaim.unsupported_verb(claim):
            raise Inconclusive(
                "claim_writer answered invalid_request — the pinned "
                "release predates the claim contract the ownerless "
                f"window is opened through: {claim}"
            )
        verdict = claim.get("result")
        if verdict == "claimed_shared":
            failures.append(
                "the preempting claim joined a live holder of the same "
                "token — a leaked staging attachment shares the "
                f"induction token: {claim}"
            )
            raise Abort
        if verdict != "done":
            failures.append(
                f"the preempting claim was refused: {claim} — "
                "claim_writer must preempt unconditionally"
            )
            raise Abort
        release = induct_io.request({"op": "release_writer"})
        evidence["release"] = release
        if claim_reclaim.unsupported_verb(release):
            raise Inconclusive(
                "release_writer answered invalid_request — the pinned "
                "release predates the claim lifecycle the hand-back "
                f"rides on: {release}"
            )
        if release.get("result") != "done":
            failures.append(
                f"the claim hand-back was refused: {release}"
            )
            raise Abort
        window = induct_io.request({"op": "step", "dt": 0})
        evidence["window_probe"] = window
        kind = failover.probe_kind(window)
        if kind not in ("unclaimed", "fenced"):
            raise Inconclusive(
                "the plant server answers neither a grant nor a named "
                "fencing or unclaimed verdict on a mutation with no "
                "claim standing — the pinned release predates the "
                f"verdict vocabulary the re-arm contract reads: "
                f"{window}"
            )
        if not fail_closed(kind):
            failures.append(
                "the field accepted a third-party mutation with no "
                f"claim standing: {window}"
            )
            raise Abort
        if kind == "fenced" and claim_reclaim.verdict_owner(window) is None:
            raise Inconclusive(
                "a mutation with no claim standing is refused as a "
                "plain fencing verdict naming no owner — the pinned "
                "release predates the unclaimed-field verdict the "
                f"recoverable ownerless window is read through: "
                f"{window}"
            )
        digest_entries.append(
            {
                "phase": "induction",
                "claim": verdict,
                "hand_back": release.get("result"),
                "window": kind,
            }
        )

        # Phase 3 — the watch: the recorded owner's next scans re-arm
        # the claim inline — their writes landing, its role and
        # journals unmoved, its fencing-loss ledger empty, the field
        # fail-closed to third-party probes throughout. The watch
        # itself refuses any probe answer that is not one of the two
        # fail-closed shapes, so a granted mutation aborts the run
        # here rather than reaching the re-arm's own verdict.
        watch = rearm_watch(
            plant_io,
            verdict_io,
            owner_url,
            tracker_url,
            cmd,
            losses0,
            failures,
            evidence,
        )
        digest_entries.append({"phase": "watch", "watch": watch})

        # Phase 4 — the window's journal audit: above the floors,
        # neither peer's served journal nor either declared durable
        # journal file gained a `field_claim_lost` or a `role_changed`
        # record. An inline re-arm journals nothing: it is a
        # recovered window, not an episode.
        audit = {}
        for name, url in (("owner", owner_url), ("tracker", tracker_url)):
            added = pair.get(
                f"{url}/journal", "GET /journal", failures
            )[floors[name]:]
            audit[name] = {
                "losses": len(stranded_rejoin.lost_entries(added)),
                "walk": stranded_rejoin.role_walk(added),
            }
            if audit[name]["losses"] or audit[name]["walk"]:
                failures.append(
                    f"the {name} peer journaled a fencing loss or a "
                    "role transition across the ownerless window — the "
                    "unclaimed refusal was read as supersession: "
                    f"losses {audit[name]['losses']} "
                    f"walk {audit[name]['walk']}"
                )
        for name, files in (
            ("owner_file", rig.duty_files),
            ("tracker_file", rig.standby_files),
        ):
            added = journal_entries(files["journal_file"])[floors[name]:]
            audit[name] = {
                "losses": len(stranded_rejoin.lost_entries(added)),
                "walk": stranded_rejoin.role_walk(added),
            }
            if audit[name]["losses"] or audit[name]["walk"]:
                failures.append(
                    "the declared durable journal gained a fencing "
                    "loss or a role transition across the ownerless "
                    "window — the served record it must mirror shows "
                    f"none: losses {audit[name]['losses']} "
                    f"walk {audit[name]['walk']}"
                )
        if failures:
            raise Abort
        digest_entries.append({"phase": "audit", **audit})

        # Phase 5 — the re-armed claim is real: a third-party probe
        # is fenced naming the recorded owner's token again, and a
        # foreign attachment's conditional claim and write probes stay
        # fenced beside it.
        rearmed = verdict_io.request({"op": "step", "dt": 0})
        evidence["rearm_probe"] = rearmed
        if not claim_reclaim.mutation_fenced(rearmed):
            failures.append(
                "the field never re-armed the recorded owner's claim "
                f"— a third-party probe answered {rearmed}"
            )
            raise Abort
        rearm_owner = claim_reclaim.verdict_owner(rearmed)
        seated = owner_word(rearm_owner, owner_token)
        if tamper == "expect-foreign":
            # The doctored expectation: the re-armed claim came to
            # rest under the foreign claimant. The honest window
            # re-armed it through the owner's own conditional grant,
            # so the assertion cannot stand.
            if seated != "foreign":
                failures.append(
                    f"{TAMPER_EVIDENCE} — the re-armed claim names the "
                    f"recorded owner ({seated}), the owner's own "
                    "conditional grant is what took the field back"
                )
        elif seated != "owner":
            failures.append(
                "the re-armed claim did not come to rest under the "
                f"recorded owner — the fencing verdict names "
                f"{rearm_owner}: {rearmed}"
            )
            raise Abort
        foreign_ensure = foreign_io.request(
            {"op": "ensure_writer", "owner": FOREIGN_OWNER}
        )
        evidence["foreign_ensure"] = foreign_ensure
        if claim_reclaim.unsupported_verb(foreign_ensure):
            raise Inconclusive(
                "ensure_writer answered invalid_request — the pinned "
                "release predates the conditional-claim verb the "
                f"re-arm runs on: {foreign_ensure}"
            )
        admitted = foreign_ensure.get("result") in ("done", "claimed_shared")
        if admitted:
            # A granted foreign claim would stand in the field's way:
            # hand it straight back so the restore and the owner's
            # claim are untouched by it.
            try:
                foreign_io.request({"op": "release_writer"})
            except Exception:
                pass
        if not admitted and not claim_reclaim.mutation_fenced(
            foreign_ensure
        ):
            raise Inconclusive(
                "a foreign attachment's conditional claim answered "
                "neither a grant nor a fencing refusal — the pinned "
                "release predates the grant's contract shape: "
                f"{foreign_ensure}"
            )
        foreign_write = foreign_write_probe(plant_io, probe_point, failures)
        evidence["foreign_write"] = foreign_write
        if failures:
            raise Abort
        if tamper != "expect-foreign" and admitted:
            failures.append(
                "a foreign attachment's conditional claim was admitted "
                "past the re-armed claim — the re-arm produced no real "
                f"claim: {foreign_ensure}"
            )
        if not write_fenced(foreign_write):
            failures.append(
                "a foreign attachment's write probe was not fenced "
                f"under the re-armed claim: {foreign_write}"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "rearm",
                "seated": seated,
                "foreign_ensure": "admitted" if admitted else "fenced",
                "foreign_write": "fenced"
                if write_fenced(foreign_write)
                else "landed",
            }
        )

        # Phase 6 — the owner's writes keep landing after the foreign
        # probes: the re-armed claim fences for its own owner, never
        # against it.
        sustained = []
        for _ in range(REARM_SCANS):
            owner = pair.scan(owner_url, failures)
            report = pair.get(f"{owner_url}/role", "GET /role", failures)
            sample = failover.field_read(plant_io, cmd, failures)
            image = simulate.snapshot_point(owner, cmd)
            sustained.append(
                {
                    "owner": report.get("role"),
                    "plant_tick": sample.get("tick"),
                    "field": "landed" if sample.get("value") == image
                    else "stalled",
                }
            )
            if report.get("role") != "active" or (
                sample.get("value") != image
            ):
                failures.append(
                    "the owner's writes stopped landing after the "
                    f"foreign probes — the re-armed claim fences its "
                    f"own owner: {report} carries {sample.get('value')} "
                    f"while its image reports {image}"
                )
                raise Abort
        evidence["sustained"] = sustained
        digest_entries.append({"phase": "sustained", "sustained": sustained})

        # Phase 7 — the contrast the contract discriminates: the
        # foreign attachment claims again and holds. That refusal is
        # under another standing claim, so the owner's write fences
        # and demotes it in place — the claim-reclaim leg's own shape,
        # staged here to prove the two refusals take two outcomes.
        # The field value the held-claim window must leave unmoved: no
        # owner's write lands and no owner's scan steps the plant while
        # the foreign claim stands, so the field freezes here.
        seized_field = failover.field_read(plant_io, cmd, failures)[
            "value"
        ]
        claim2 = induct_io.request(
            {"op": "claim_writer", "owner": FOREIGN_OWNER}
        )
        evidence["claim2"] = claim2
        if claim_reclaim.unsupported_verb(claim2):
            raise Inconclusive(
                "claim_writer answered invalid_request — the pinned "
                f"release predates the claim contract: {claim2}"
            )
        if claim2.get("result") != "done":
            failures.append(
                "the held foreign claim was refused: "
                f"{claim2} — claim_writer must preempt unconditionally"
            )
            raise Abort
        held = True
        seized = verdict_io.request({"op": "step", "dt": 0})
        evidence["seized"] = seized
        if not claim_reclaim.mutation_fenced(seized):
            failures.append(
                "the field accepted a third-party mutation under the "
                f"held claim: {seized}"
            )
            raise Abort
        if claim_reclaim.verdict_owner(seized) != FOREIGN_OWNER:
            failures.append(
                "the held claim is not attributed to the induction "
                f"token: {seized}"
            )
            raise Abort
        digest_entries.append(
            {"phase": "contrast", "claim": claim2.get("result"),
             "seized": "foreign"}
        )

        # Phase 8 — the fenced demotion: the owner's next scans meet
        # the fence and stand it down in place, its monitor answering
        # throughout, the field carrying no foreign write.
        demote = []
        settled = None
        for _ in range(DEMOTE_SCANS):
            pair.scan(owner_url, failures)
            report = pair.get(f"{owner_url}/role", "GET /role", failures)
            partner = claim_reclaim.try_role(tracker_url)
            probe = verdict_io.request({"op": "step", "dt": 0})
            sample = failover.field_read(plant_io, cmd, failures)
            demote.append(
                {
                    "owner": report.get("role"),
                    "owner_sync": stranded_rejoin.sync_kind(report),
                    "peer": (partner or {}).get("role"),
                    "probe_owner": owner_word(
                        claim_reclaim.verdict_owner(probe), owner_token
                    ),
                    "field_moved": sample.get("value") != seized_field,
                }
            )
            if report.get("role") == "standby":
                settled = report
                break
            if partner is None or not claim_reclaim.converged_sync(partner):
                failures.append(
                    "the tracking peer left converged standby while a "
                    "different owner's claim stood — the claim the "
                    f"fenced write must refuse: {partner}"
                )
                raise Abort
            if claim_reclaim.verdict_owner(probe) != FOREIGN_OWNER:
                failures.append(
                    "the standing claim moved off the induction token "
                    "while the owner ran demoted — the fence took "
                    f"something other than the held claim: {probe}"
                )
                raise Abort
            if sample.get("value") != seized_field:
                failures.append(
                    "the field moved under the held foreign claim — a "
                    f"foreign write landed: {sample.get('value')} over "
                    f"the seized {seized_field}"
                )
                raise Abort
        evidence["demote"] = demote
        if settled is None:
            failures.append(
                "the fenced owner never stood down — the held claim "
                f"moved the field but the role stayed {demote}"
            )
            raise Abort
        digest_entries.append({"phase": "demotion", "watch": demote})

        # Phase 9 — the journaled loss: at least one
        # `field_claim_lost` above the owner's floor, every record
        # attributing the takeover to the induction token the field's
        # own fencing verdict named, beside the fenced
        # active → demoting → standby walk.
        added = pair.get(f"{owner_url}/journal", "GET /journal", failures)[
            floors["owner"]:
        ]
        losses = stranded_rejoin.lost_entries(added)
        walk = stranded_rejoin.role_walk(added)
        if not losses:
            failures.append(
                "the held claim was silent — the fenced owner's "
                "journal recorded no field_claim_lost"
            )
            raise Abort
        if any(
            record.get("claimant") != FOREIGN_OWNER
            for _seq, record in losses
        ):
            failures.append(
                "the field_claim_lost records do not attribute the "
                "takeover to the induction token "
                f"{FOREIGN_OWNER:#x}: {losses}"
            )
            raise Abort
        if any(origin is None for _frm, _to, origin in walk):
            raise Inconclusive(
                "the served journal's role changes carry no origin "
                "attribution — the pinned release predates the walk "
                f"fields the fenced-demotion audit reads: {walk}"
            )
        if walk[:2] != [
            ("active", "demoting", "fenced"),
            ("demoting", "standby", "fenced"),
        ]:
            failures.append(
                "the fenced owner's role walk is "
                f"{walk[:2]}, expected active → demoting → standby "
                "under the fenced origin"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "loss",
                "losses": len(losses),
                "attributed": "claimant",
                "walk": walk[:2],
            }
        )

        # Phase 10 — the hand-back: the held claim is released and the
        # loss-marked ex-owner's bound reclaim takes the field back
        # under its own token, walking it standby → promoting → active
        # with no operator call.
        release2 = induct_io.request({"op": "release_writer"})
        evidence["release2"] = release2
        if claim_reclaim.unsupported_verb(release2):
            raise Inconclusive(
                "release_writer answered invalid_request — the pinned "
                f"release predates the claim lifecycle: {release2}"
            )
        if release2.get("result") != "done":
            failures.append(
                f"the held claim's hand-back was refused: {release2}"
            )
            raise Abort
        held = False
        reclaim = []
        reclaimed = None
        for _ in range(RECLAIM_SCANS):
            pair.scan(owner_url, failures)
            report = pair.get(f"{owner_url}/role", "GET /role", failures)
            reclaim.append(report.get("role"))
            if report.get("role") == "active":
                reclaimed = report
                break
        evidence["reclaim"] = reclaim
        if reclaimed is None:
            failures.append(
                "the released field was never re-seated — the "
                f"loss-marked reclaim never ran: roles {reclaim}"
            )
            raise Abort
        reseated = verdict_io.request({"op": "step", "dt": 0})
        evidence["reseated"] = reseated
        if not claim_reclaim.mutation_fenced(reseated):
            failures.append(
                "the re-seated claim does not fence third-party "
                f"mutations: {reseated}"
            )
            raise Abort
        seated_word = owner_word(
            claim_reclaim.verdict_owner(reseated), owner_token
        )
        if seated_word != "owner":
            failures.append(
                "the re-seated claim names "
                f"{claim_reclaim.verdict_owner(reseated)}, not the "
                f"launch owner: {reseated}"
            )
            raise Abort
        walk_after = stranded_rejoin.role_walk(
            pair.get(f"{owner_url}/journal", "GET /journal", failures)[
                floors["owner"]:
            ]
        )
        if ("standby", "promoting", "reclaim") not in walk_after or (
            "promoting", "active", "reclaim"
        ) not in walk_after:
            failures.append(
                "the reclaim left no reclaim-origin walk back to active "
                "on the ex-owner — an operator or restart path ran "
                f"instead: {walk_after}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "reclaim",
                "watch": reclaim,
                "seated": seated_word,
                "walk": walk_after,
            }
        )

        # Phase 11 — the settle: tracking-first ticks prove the pair
        # holds identical images, the reclaimed owner stays `active`,
        # the declared standby returns to `tracking`, and the
        # re-seated claim passes the owner's own writes — a
        # holderless field would fence them.
        handover = []
        for _ in range(SETTLE_TICKS):
            _tracked, owner = rig.tick(
                tracker_url,
                owner_url,
                failures,
                diverged="the settled pair's images diverged at tick "
                "{tick} — the reclaim was not bumpless",
            )
            handover.append(owner["tick"])
        owner_role = pair.get(f"{owner_url}/role", "GET /role", failures)
        tracker_role = pair.get(f"{tracker_url}/role", "GET /role", failures)
        if owner_role.get("role") != "active":
            failures.append(
                "the reclaimed owner never settled active — GET "
                f"/role answers {owner_role}"
            )
            raise Abort
        if not claim_reclaim.tracking(tracker_role):
            failures.append(
                "the declared standby did not return to tracking "
                f"standby — GET /role answers {tracker_role}"
            )
            raise Abort
        held_now = failover.field_read(plant_io, cmd, failures)["value"]
        if held_now != simulate.snapshot_point(owner, cmd):
            failures.append(
                "the re-seated claim fences the owner's own writes — "
                f"the field carries {held_now} while its image reports "
                f"{simulate.snapshot_point(owner, cmd)}"
            )
            raise Abort
        post = verdict_io.request({"op": "step", "dt": 0})
        evidence["post_probe"] = post
        if not claim_reclaim.mutation_fenced(post):
            failures.append(
                "the settled claim does not fence third-party "
                f"mutations: {post}"
            )
            raise Abort
        if owner_word(
            claim_reclaim.verdict_owner(post), owner_token
        ) != "owner":
            failures.append(
                "the settled claim names "
                f"{claim_reclaim.verdict_owner(post)} — never re-seated "
                f"under the launch owner: {post}"
            )
            raise Abort
        evidence["final_tick"] = handover[-1]
        digest_entries.append(
            {
                "phase": "settle",
                "ticks": handover,
                "owner_role": owner_role.get("role"),
                "tracker_role": tracker_role.get("role"),
                "field": held_now,
                "claim": "owner",
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if held and rig is not None:
            # Best effort: a stranded foreign hold keeps fencing the
            # owner's re-arm and its reclaim. Drop the staging
            # attachment's own hold; when that connection is already
            # gone, re-take the induction token through a fresh
            # attachment and release — only while the verdict still
            # names it foreign, so a finished reclaim is never
            # re-preempted.
            if induct_io is not None:
                try:
                    induct_io.request({"op": "release_writer"})
                except Exception:
                    try:
                        fixer = simulate.PlantClient(rig.plant_addr)
                        try:
                            probe = fixer.request({"op": "step", "dt": 0})
                            if claim_reclaim.verdict_owner(
                                probe
                            ) == FOREIGN_OWNER:
                                fixer.request(
                                    {
                                        "op": "claim_writer",
                                        "owner": FOREIGN_OWNER,
                                    }
                                )
                                fixer.request({"op": "release_writer"})
                        finally:
                            fixer.close()
                    except Exception:
                        pass  # an unreachable plant is the pass's own
                             # verdict
        if rig is not None and owner_url is not None:
            # Best effort: the launch role layout for the legs behind
            # this one. Give a pending reclaim a few scans to land —
            # the freed field may already be re-seating — then the
            # documented operator promote is the fallback on a wedge;
            # a peer moved off standby demotes back. A clean pass
            # moved nothing, so neither fires.
            try:
                for _ in range(RECLAIM_SCANS):
                    if (try_role(owner_url) or {}).get(
                        "role"
                    ) == "active":
                        break
                    try_scan(owner_url)
                if (try_role(owner_url) or {}).get("role") != "active":
                    pair.request(f"{owner_url}/promote", {})
            except Exception:
                pass
            try:
                if (try_role(tracker_url) or {}).get("role") != (
                    "standby"
                ):
                    pair.request(f"{tracker_url}/demote", {})
            except Exception:
                pass
            rig.close()
        for client in (verdict_io, induct_io, foreign_io):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
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
        choices=["expect-foreign"],
        help="doctor the leg's own re-arm expectation — asserting the "
        "re-armed claim came to rest under the foreign claimant — so "
        "the pass must fail naming the honest standing-owner re-arm",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = unclaimed_rearm_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"unclaimed-rearm: {TAMPER_EVIDENCE} — an inconclusive "
                "run offers the doctored case no evidence"
            )
            return 1
        # The digest line carries only the stable reason — the run's
        # own verdicts and minted tokens report on stderr, where two
        # identical passes need not share them.
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"unclaimed-rearm: inconclusive — {detail}")
        print(f"unclaimed-rearm-digest inconclusive — {reason}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"unclaimed-rearm: {line}")
        return 1
    for failure in failures:
        eprint(f"unclaimed-rearm: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"unclaimed-rearm: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"unclaimed-rearm-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the preempt-and-release opened the "
        "ownerless window behind the owner's live connection and its "
        "next scan re-armed the claim inline, its writes landing with "
        "no fencing loss, no role walk, and nothing journaled on "
        "either peer, the foreign conditional-claim and write probes "
        "stayed fenced, the standing-claim refusal still fenced and "
        "demoted the owner, and the pair reconverged to its launch "
        f"roles at tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())