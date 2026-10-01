#!/usr/bin/env python3
"""The observed-claimant journal leg for the reference plant — the
consumer-side proof that a foreign preempt-and-release a peer only
ever meets through its claim probes still leaves exactly one
attributed `field_claim_observed` record on that peer's journal —
deduplicated on the observed owner token — beside the unchanged
claim-loss, reclaim, and role-change entries, with the same record
landing in seq order in the manifest-declared durable journal file
(WW-ENG-003, WW-LCM-001 — the #987 observed-owner journal contract
the rig's claim-observation scenario pins in-workspace, mirrored at
the customer boundary).

The claim-reclaim leg (`ci/legs/claim_reclaim.py`) proves the
standing claim's preempt-and-reclaim lifecycle against the consumer
pair — the foreign `claim_writer` preempt, the attributed
`field_claim_lost` and demote-in-place, and the released field's
bound conditional reclaim. This leg stages the handover the recorded
loss cannot see on its own: a claimant that took the claim between
the loss and the reclaim, a foreign token the demoted ex-owner's
bound re-grant probes meet only as a refusal — the peer never fenced
a write of its own under the second claim, so no `field_claim_lost`
can fire for it. Before the contract the episode absorbed silently:
the audit named a claimant the field no longer stood under. A
refused conditional grant now journals one
`field_claim_observed{point, claimant}` per distinct standing-owner
token the refusal names, seeded by the recorded loss's claimant so
the loss's attribution never double-records. Reusing the
foreign-claim staging the claim-reclaim leg drives, the run:

- converges the declared pair to `tracking` on the released images —
  the launched active's startup claim line recording the owner token
  the standing claim was taken under — and probes the lifecycle
  surface with a foreign `ensure_writer`, refused `fenced` naming
  the standing owner;
- preempts the standing claim with a dedicated induction
  attachment's `claim_writer` — the unconditional grant the
  claim-reclaim leg's rogue claim drives — and holds it while the
  superseded owner's first fenced write demotes it in place, the
  served journal gaining one `field_claim_lost` attributed to the
  induction token beside the `active → demoting → standby` walk the
  `fenced` origin stamps;
- hands the claim to a second foreign attachment's `claim_writer`
  while the demoted ex-owner probes — its bound re-grant and orphan
  re-arm refusing under the induction claimant and journaling
  nothing, the recorded loss's claimant seeding the dedup — and
  holds the observed claim across a driven window of refused
  probes;
- asserts the observing peer's served journal carries exactly one
  `field_claim_observed` naming the standing owner token — one
  record per observed owner token, not one per probe, the induction
  claimant never re-observed — then has the observed attachment
  release the claim and watches the loss-marked bound reclaim
  re-seat the ex-owner `standby → promoting → active` under the
  `reclaim` origin, no operator call having run;
- audits the episode in seq order — the attributed loss, the one
  observed-claimant record, the reclaim walk — against the served
  journal and the manifest-declared durable journal file, while the
  tracking peer's journal carries no loss, no observed record, and
  no role change;
- restores the pair's launch roles — the manifest-declared duty
  controller `active` and writing again, its standby `tracking`,
  the claim standing under the owner's own token.

The contract postdates the pinned release line: where the launched
tooling predates it — no recorded owner token, the claim lifecycle
verbs unanswered, the preemption lever refused, the fenced
supersession never demoting, the loss or its attribution
unjournaled, or the refused probes journaling no record — the run's
own evidence is the pre-contract shape and the leg reports
`claim-observed-digest inconclusive` rather than asserting until
the manifest repins a release carrying the contract.

Usage:

    claim_observed.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `claim-observed-digest <sha256>` line prints — the
check runs two passes and compares them
(`claim-observed-nondeterministic`). A contract violation reports
`claim-observed: …` lines on stderr and exits 1 — the check's
`claim-observed-failed`. `--tamper expect-silence` doctors the leg's
own expectation to the pre-contract shape — asserting the observed
episode may journal nothing — so the leg proves its audit fires on
the honest attributed record rather than passing an unexercised
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
import failover
import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the observed episode may
# journal nothing — the silent absorption the contract closed —
# must surface the named diagnostic on the honest attributed
# record rather than passing an unexercised contract.
LEG = {
    "order": 390,
    "title": "the observed-claimant journal leg",
    "passes": "claim-observed",
    "tampers": [
        {
            "name": "expect-silence",
            "passed": "an expect-silence case passed the claim-observed leg",
            "missed": "the expect-silence case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted the observed claimant unrecorded"],
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


# The claim tokens the leg's two foreign attachments assert — small
# fixed tokens that cannot collide with a controller's per-process
# minted token, distinct from the claim-reclaim leg's induction
# token so a diagnostic never confuses the legs' claimants. The
# induction token takes the preempt the recorded loss attributes;
# the observed token takes the handover the demoted ex-owner only
# ever meets through its refused probes.
CLAIM_INDUCTION = 0xF021
CLAIM_OBSERVED = 0xF022

# The driven-scan bounds the episode's phases run: the seeded window
# the induction claimant's refusals span without journaling, and the
# dedup window the observed claim's refusals span while the journaled
# count must stay at one.
SEEDED_SCANS = 2
OBSERVE_SCANS = 4

# The role walk the episode stamps on the observing peer — the
# fenced demote-in-place, then the unattended reclaim — with the
# switch origins the peer's own transitions carry.
ROLE_WALK = [
    ("active", "demoting", "fenced"),
    ("demoting", "standby", "fenced"),
    ("standby", "promoting", "reclaim"),
    ("promoting", "active", "reclaim"),
]


def journal_entries(path):
    """A `--journal-file`'s entry records in file order — the durable
    half of the audit."""
    return [
        record
        for kind, record in pair.journal_records(path)
        if kind == "entry"
    ]


def observed_entries(entries):
    """The `field_claim_observed` records of a journal entry list —
    `(seq, record)` pairs in journal order."""
    return [
        (entry["seq"], entry["event"]["field_claim_observed"])
        for entry in entries
        if "field_claim_observed" in entry.get("event", {})
    ]


def lost_entries(entries):
    """The `field_claim_lost` records of a journal entry list —
    `(seq, record)` pairs in journal order."""
    return [
        (entry["seq"], entry["event"]["field_claim_lost"])
        for entry in entries
        if "field_claim_lost" in entry.get("event", {})
    ]


def role_walk(entries):
    """The `role_changed` stream of a journal entry list —
    `(from, to, origin)` per entry, in `seq` order; the origin reads
    `None` on an entry predating the attribution fields."""
    return [
        (change["from"], change["to"], change.get("origin"))
        for entry in entries
        if "role_changed" in entry.get("event", {})
        for change in [entry["event"]["role_changed"]]
    ]


def claim_grant(client, token, what, evidence):
    """One unconditional `claim_writer` under `token` on the claim
    attachment — the preempt half of the leg's staging. `done` takes
    the field's claim; every other answer classifies: a refusal or an
    unsupported verb is the release predating the preemption lever
    the episode needs, a shared join a leaked hold contaminating the
    staging."""
    claim = client.request({"op": "claim_writer", "owner": token})
    evidence[what] = claim
    result = claim.get("result")
    if result == "done":
        return claim
    if result == "claimed_shared":
        raise Abort(
            f"the {what} claim answered claimed_shared — a live "
            f"attachment already holds the leg's fixed token "
            f"{token:#x}: {claim}"
        )
    raise Inconclusive(
        f"the {what} claim answered {claim} — the consumer harness "
        "admits no unconditional preemption lever, or the pinned "
        "plant predates it"
    )


def claim_observed_pass(args, tamper):
    """The claim-observed run: converge, induct, demote, hand the
    claim to the observed claimer, hold the dedup window, release
    into the reclaim, audit, restore. Returns `(digest_entries,
    evidence, failures)`; raises `Inconclusive` where the pinned
    release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "claim-observed leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = verdict_io = induction_io = observed_io = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io
        # The claim attachments the episode stages through — the
        # rig's own client stays read-only, so every claim belongs
        # to these two connections' holds. `verdict_io` runs the
        # third-party mutation probes and never holds a claim — the
        # staging discipline the claim-reclaim leg drives.
        verdict_io = simulate.PlantClient(rig.plant_addr)
        induction_io = simulate.PlantClient(rig.plant_addr)
        with open(args.model) as handle:
            model = json.load(handle)
        points = failover.signal_points(model)
        if points is None:
            raise Abort(
                "the emitted model declares no p101-cmd/level-primary "
                "signal points — the leg has no field points to probe"
            )
        cmd = points["cmd"]

        # Phase 1 — convergence and the contract surface: the
        # launched active's recorded claim token, the lifecycle verbs
        # answered, the standing claim fencing foreign grants and
        # naming its owner, and the durable journal file the
        # manifest declares. Each absence is the release predating
        # the contract, never a violation of it.
        converged = rig.converge(failures)
        owner = converged["owner"]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )
        evidence["converged"] = converged["ticks"][-1]
        owner_token = failover.owner_token(rig.duty_preamble)
        if owner_token is None:
            raise Inconclusive(
                "the launched active recorded no claim line — the "
                "pinned release claims only on promotion, predating "
                "the claim lifecycle the observed-claimant contract "
                "rides on"
            )
        if rig.duty_files.get("journal_file") is None:
            raise Inconclusive(
                "the manifest's pair declares no duty journal_file — "
                "the durable half of the observation audit is absent"
            )
        verbs = induction_io.request(
            {"op": "ensure_writer", "owner": CLAIM_INDUCTION}
        )
        evidence["verbs"] = verbs
        if claim_reclaim.unsupported_verb(verbs):
            raise Inconclusive(
                f"a foreign ensure_writer answered {verbs} — the "
                "pinned release predates the claim lifecycle verbs "
                "the observed-claimant contract rides on"
            )
        if not claim_reclaim.mutation_fenced(verbs):
            failures.append(
                "a foreign token's ensure_writer was not refused "
                "fenced — the conditional grant preempted or joined "
                f"a claim it must not reach: {verbs}"
            )
            raise Abort
        if claim_reclaim.verdict_owner(verbs) is None:
            raise Inconclusive(
                "the fencing verdict names no standing owner — the "
                "pinned release predates the verdict attribution "
                "the observed record's claimant reads: "
                f"{verbs}"
            )
        if claim_reclaim.verdict_owner(verbs) != owner_token:
            failures.append(
                "the fencing verdict attributes the standing claim "
                f"to {claim_reclaim.verdict_owner(verbs)}, not the recorded owner "
                f"token {owner_token:#x}: {verbs}"
            )
            raise Abort

        # The audit's floors: each peer's served journal position and
        # the duty's durable journal file count at baseline.
        journal0 = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        peer_journal0 = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        file_floor = len(journal_entries(rig.duty_files["journal_file"]))

        # Phase 2 — the induction preempt: the unconditional claim
        # the claim-reclaim leg's rogue claim drives, held while the
        # superseded owner's first fenced write demotes it in place.
        claim_grant(induction_io, CLAIM_INDUCTION, "induction", evidence)
        seized = verdict_io.request({"op": "step", "dt": 0})
        evidence["seized"] = seized
        if not claim_reclaim.mutation_fenced(seized):
            failures.append(
                "the induction claim's preempt did not fence the "
                f"field — a foreign attachment's probe answered "
                f"{seized}"
            )
            raise Abort
        if claim_reclaim.verdict_owner(seized) != CLAIM_INDUCTION:
            if claim_reclaim.verdict_owner(seized) is None:
                raise Inconclusive(
                    "the post-preemption fencing verdict names no "
                    "standing owner — the pinned release predates "
                    "the verdict attribution the contract's "
                    "claimants read"
                )
            failures.append(
                "the fencing verdict attributes the preempted claim "
                f"to {claim_reclaim.verdict_owner(seized)}, not the induction "
                f"token {CLAIM_INDUCTION:#x}: {seized}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "induction",
                "claim": "granted",
                "seized": failover.probe_kind(seized),
                "seized_owner": claim_reclaim.verdict_owner(seized),
            }
        )

        # Phase 3 — the demote-in-place watch: the superseded owner's
        # detection scans walk demoting to standby with the monitor
        # answering throughout. A refused first scan — the fenced
        # write fatal rather than degraded — is the claim-only shape
        # the demote-in-place contract postdates.
        status, body = pair.request(
            f"{duty_url}/scan", {"scans": 1}
        )
        if status != 200:
            if "fenc" in str(body):
                raise Inconclusive(
                    "the superseded owner's fenced scan refused "
                    f"{status} {body} — the pinned release predates "
                    "the demote-in-place contract the observed "
                    "episode's loss mark rides on"
                )
            failures.append(
                "the superseded owner's first fenced scan answered "
                f"{status} {body} — the preemption took the monitor "
                "down, not the degraded demote the contract settles"
            )
            raise Abort
        roles = []
        settled = None
        for _ in range(claim_reclaim.WATCH_SCANS):
            report = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            roles.append(report.get("role"))
            if report.get("role") == "standby":
                settled = report
                break
            pair.scan(duty_url, failures)
        evidence["demote_watch"] = roles
        if settled is None:
            failures.append(
                "the superseded owner never demoted — the induction "
                "claim moved the claim but the role stayed "
                f"{roles}"
            )
            raise Abort

        # The recorded loss: exactly one field_claim_lost above the
        # floor, attributed to the induction token — the attribution
        # that seeds the observation dedup.
        journal1 = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        added = journal1[len(journal0) :]
        losses = lost_entries(added)
        evidence["losses"] = losses
        if not losses:
            raise Inconclusive(
                "the demote-in-place left no field_claim_lost on "
                "the owner's journal — the loss record the "
                "observation orders after never ran; the pinned "
                "release predates the contract"
            )
        if len(losses) != 1:
            failures.append(
                "expected exactly one field_claim_lost above the "
                f"journal floor, found {len(losses)} — one record "
                "per held claim, not one per fenced write"
            )
            raise Abort
        loss_seq, loss = losses[0]
        if "claimant" not in loss:
            raise Inconclusive(
                "the journaled fencing loss names no claimant — "
                "the loss attribution that seeds the dedup; the "
                "pinned release predates the contract: "
                f"{loss}"
            )
        if loss.get("claimant") != CLAIM_INDUCTION:
            failures.append(
                "the journaled fencing loss attributes the "
                f"takeover to {loss.get('claimant')}, not the "
                f"induction token {CLAIM_INDUCTION:#x}"
            )
            raise Abort
        evidence["lost_seq"] = loss_seq
        digest_entries.append(
            {
                "phase": "demote",
                "watch": roles,
                "loss": {"seq": loss_seq, "record": loss},
            }
        )

        # Phase 4 — the seeded window: the demoted ex-owner's bound
        # re-grant and orphan re-arm probes refuse under the
        # induction claimant across driven scans, journaling nothing
        # — the recorded loss's claimant seeding the dedup so the
        # episode never double-records.
        for _ in range(SEEDED_SCANS):
            pair.scan(duty_url, failures)
        seeded = observed_entries(
            pair.get(f"{duty_url}/journal", "GET /journal", failures)[
                len(journal0) :
            ]
        )
        if seeded:
            failures.append(
                "the induction claimant re-observed through the "
                "dedup seed — the refused probes journaled "
                f"{seeded} where the recorded field_claim_lost "
                "already attributes that claimant's episode"
            )
            raise Abort
        digest_entries.append(
            {"phase": "seeded", "scans": SEEDED_SCANS, "observed": 0}
        )

        # Phase 5 — the observed claimer: the second foreign
        # attachment preempts the induction claim and holds it — the
        # handover the demoted ex-owner can only meet through its
        # refused conditional probes.
        observed_io = simulate.PlantClient(rig.plant_addr)
        claim_grant(observed_io, CLAIM_OBSERVED, "observed", evidence)
        seized2 = verdict_io.request({"op": "step", "dt": 0})
        evidence["seized2"] = seized2
        if not claim_reclaim.mutation_fenced(seized2):
            failures.append(
                "the observed claim's handover did not fence the "
                f"field — a foreign attachment's probe answered "
                f"{seized2}"
            )
            raise Abort
        if claim_reclaim.verdict_owner(seized2) != CLAIM_OBSERVED:
            failures.append(
                "the fencing verdict attributes the handed-over "
                f"claim to {claim_reclaim.verdict_owner(seized2)}, not the "
                f"observed token {CLAIM_OBSERVED:#x}: {seized2}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "handover",
                "claim": "granted",
                "seized": failover.probe_kind(seized2),
                "seized_owner": claim_reclaim.verdict_owner(seized2),
            }
        )

        # Phase 6 — the observation window: each driven scan runs
        # the demoted peer's refused conditional probes, and the
        # served journal must hold exactly one field_claim_observed
        # naming the standing owner — the count never growing once
        # it lands — while both peers stay standby.
        counts = []
        window = []
        observed = []
        for _ in range(OBSERVE_SCANS):
            pair.scan(duty_url, failures)
            added = pair.get(
                f"{duty_url}/journal", "GET /journal", failures
            )[len(journal0) :]
            observed = observed_entries(added)
            counts.append(len(observed))
            duty_role = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            peer_role = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            window.append(
                {
                    "duty": duty_role.get("role"),
                    "peer": peer_role.get("role"),
                    "observed": len(observed),
                }
            )
            if duty_role.get("role") != "standby":
                failures.append(
                    "the marked ex-owner left standby while a "
                    "different-owner claim stood — the bound grant "
                    "took what it must refuse: "
                    f"{duty_role}"
                )
                raise Abort
            if peer_role.get("role") != "standby":
                failures.append(
                    "the tracking peer reported a role change "
                    f"through the observed claim: {peer_role}"
                )
                raise Abort
        evidence["window"] = window
        named = [record for _seq, record in observed]
        standing = [
            record
            for record in named
            if record.get("claimant") == CLAIM_OBSERVED
        ]
        reseeded = [
            record
            for record in named
            if record.get("claimant") == CLAIM_INDUCTION
        ]
        if not observed:
            raise Inconclusive(
                "the demoted peer probed the observed claim across "
                f"{OBSERVE_SCANS} refused scans and journaled no "
                "field_claim_observed — the pre-contract absorption "
                "the contract exists to close; the pinned release "
                "predates the observed-claimant contract"
            )
        if any("claimant" not in record for record in named):
            failures.append(
                "a journaled field_claim_observed names no "
                f"claimant — the record the contract stamps is "
                f"always attributed: {observed}"
            )
        elif reseeded:
            failures.append(
                "the journal observed the induction claimant "
                f"{CLAIM_INDUCTION:#x} again — the recorded loss "
                "already attributes it, so the seeded dedup must "
                "journal nothing further for that token"
            )
        elif not standing:
            failures.append(
                "the journaled field_claim_observed names "
                f"{[record.get('claimant') for record in named]}, "
                f"not the standing owner {CLAIM_OBSERVED:#x}"
            )
        elif len(observed) != 1 or len(standing) != 1:
            failures.append(
                "expected exactly one field_claim_observed above "
                f"the journal floor, found {len(observed)} — the "
                "contract is one record per observed token, not "
                "one per refused probe"
            )
        elif counts[-1] != 1 or any(count > 1 for count in counts):
            failures.append(
                "the field_claim_observed count grew across the "
                f"dedup window {counts} — repeated refused probes "
                "must journal nothing further for the same token"
            )
        if failures:
            raise Abort
        observed_seq = observed[0][0]
        if observed[0][1].get("point") != loss.get("point"):
            failures.append(
                "the observed record attributes through point "
                f"{observed[0][1].get('point')} where the recorded "
                f"loss stands on {loss.get('point')} — the "
                "refused grant probed the claim domain the "
                "fenced write marked"
            )
            raise Abort
        evidence["observed_seq"] = observed_seq
        evidence["observed_claimant"] = CLAIM_OBSERVED
        digest_entries.append(
            {
                "phase": "observed",
                "counts": counts,
                "record": observed[0][1],
                "seq": observed_seq,
            }
        )

        if tamper == "expect-silence":
            # The doctored expectation — the leg asserts the
            # observed episode may journal nothing, the silent
            # absorption the contract closed. The honest attributed
            # record must fail it, naming the claimant it kept.
            failures.append(
                "the doctored expectation wanted the observed "
                "claimant unrecorded — the honest run journaled "
                f"field_claim_observed naming {CLAIM_OBSERVED:#x}"
            )
            raise Abort

        # Phase 7 — the release and the reclaim watch: the observed
        # attachment hands the claim back, and the loss-marked bound
        # re-grant lands the first scan the field stands unclaimed —
        # re-seating the claim under the owner's token and walking
        # standby → promoting → active with no operator call.
        released = observed_io.request({"op": "release_writer"})
        evidence["release"] = released
        if released.get("result") != "done":
            failures.append(
                "the observed claim's release_writer refused: "
                f"{released}"
            )
            raise Abort
        reclaim = []
        promoted = None
        for _ in range(claim_reclaim.RECONVERGE_SCANS):
            pair.scan(duty_url, failures)
            report = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            reclaim.append(report.get("role"))
            if report.get("role") == "active":
                promoted = report
                break
        evidence["reclaim"] = reclaim
        if promoted is None:
            failures.append(
                "the released field was never re-seated — the "
                "loss-marked bound reclaim never ran or refused "
                f"the unclaimed field: reported roles {reclaim}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "reclaim",
                "release": "done",
                "watch": reclaim,
                "promoted": promoted,
            }
        )

        # Phase 8 — the launch roles restored: the re-seated owner
        # writes again, the tracking peer reconverges on its
        # owned-line checkpoints, and the standing claim is the
        # owner's own again — a foreign probe fenced naming its
        # token, the owner-token join answering claimed_shared.
        handover = []
        for _ in range(pair.HANDOVER_TICKS):
            _tracked, owner = rig.tick(
                standby_url,
                duty_url,
                failures,
                diverged="the restored pair's images diverged at "
                "tick {tick} — the reclaim was not bumpless",
            )
            handover.append(owner["tick"])
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        peer_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active":
            failures.append(
                "the re-seated owner never settled active — "
                f"GET /role answers {duty_role}"
            )
            raise Abort
        if not claim_reclaim.tracking(peer_role):
            failures.append(
                "the tracking peer never reconverged after the "
                f"reclaim — GET /role answers {peer_role}"
            )
            raise Abort
        post_step = verdict_io.request({"op": "step", "dt": 0})
        if not claim_reclaim.mutation_fenced(post_step):
            failures.append(
                "the restored claim does not fence foreign "
                f"probes: {post_step}"
            )
            raise Abort
        post_shared = observed_io.request(
            {"op": "ensure_writer", "owner": owner_token}
        )
        if (
            post_shared.get("result") != "claimed_shared"
            or post_shared.get("owner") != owner_token
        ):
            failures.append(
                "the restored claim does not name the field "
                f"owner's token — ensure_writer answered "
                f"{post_shared}"
            )
            raise Abort
        post_release = observed_io.request({"op": "release_writer"})
        if post_release.get("result") != "done":
            failures.append(
                "the restore probe's release_writer refused: "
                f"{post_release}"
            )
            raise Abort
        held = failover.field_read(plant_io, cmd, failures)["value"]
        if held != simulate.snapshot_point(owner, cmd):
            failures.append(
                "the restored field owner is not writing — the "
                f"field carries {held} while its image reports "
                f"{simulate.snapshot_point(owner, cmd)}"
            )
            raise Abort
        evidence["restored_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "restore",
                "ticks": handover,
                "duty_role": duty_role,
                "standby_role": peer_role,
                "post_probe": failover.probe_kind(post_step),
                "post_shared": post_shared.get("result"),
                "field": held,
            }
        )

        # Phase 9 — the episode audit on the observing peer's served
        # journal above the floor: the seq order intact, the
        # attributed loss first, exactly one observed-claimant
        # record between it and the reclaim walk, the role walk the
        # unchanged fenced-and-reclaim sequence, and the tracking
        # peer's journal carrying no loss, no observation, and no
        # role change.
        journal_final = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        added = journal_final[len(journal0) :]
        seqs = [entry.get("seq") for entry in added]
        if any(not isinstance(seq, int) for seq in seqs) or (
            seqs != sorted(seqs)
        ):
            failures.append(
                "the observing peer's served journal is not in "
                f"seq order above the floor: {seqs}"
            )
        walk = role_walk(added)
        if any(origin is None for _from, _to, origin in walk):
            raise Inconclusive(
                "the episode's role walk carries no switch origin "
                "— the reclaim/fenced attribution the audit names "
                "the entries by; the pinned release predates the "
                "switch-attribution contract the observed record "
                "stands beside"
            )
        if walk != ROLE_WALK:
            failures.append(
                "the episode's role walk is "
                f"{walk}, expected {ROLE_WALK} — the claim/reclaim "
                "and role-change entries the observed record "
                "stands beside changed"
            )
        promoting_seqs = [
            entry["seq"]
            for entry in added
            if entry.get("event", {}).get("role_changed")
            == {"from": "standby", "to": "promoting", "origin": "reclaim"}
        ]
        if not (loss_seq < observed_seq < (promoting_seqs or [0])[0]):
            failures.append(
                "the served journal orders the episode wrong — "
                f"field_claim_lost at seq {loss_seq}, "
                f"field_claim_observed at seq {observed_seq}, "
                "standby→promoting at "
                f"{(promoting_seqs or ['absent'])[0]} — the "
                "observation must sit between the loss and the "
                "reclaim walk"
            )
        peer_added = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )[len(peer_journal0) :]
        peer_disturbed = [
            entry["event"]
            for entry in peer_added
            if any(
                kind in entry.get("event", {})
                for kind in (
                    "field_claim_lost",
                    "field_claim_observed",
                    "role_changed",
                )
            )
        ]
        if peer_disturbed:
            failures.append(
                "the tracking peer journaled the episode it never "
                f"owned — {peer_disturbed}"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "lost_seq": loss_seq,
                "observed_seq": observed_seq,
                "walk": walk,
                "peer_disturbed": peer_disturbed,
            }
        )

        # Phase 10 — the durable half: the manifest-declared journal
        # file carries the same record in seq order — the
        # observation the monitor serves is the observation that
        # persists.
        file_entries = journal_entries(rig.duty_files["journal_file"])[
            file_floor:
        ]
        file_seqs = [entry.get("seq") for entry in file_entries]
        if any(not isinstance(seq, int) for seq in file_seqs) or (
            file_seqs != sorted(file_seqs)
        ):
            failures.append(
                "the durable journal file is not in seq order "
                f"above the floor: {file_seqs}"
            )
        file_observed = [
            index
            for index, entry in enumerate(file_entries)
            if "field_claim_observed" in entry.get("event", {})
        ]
        file_named = [
            index
            for index in file_observed
            if file_entries[index]["event"]["field_claim_observed"].get(
                "claimant"
            )
            == CLAIM_OBSERVED
        ]
        file_lost = [
            index
            for index, entry in enumerate(file_entries)
            if "field_claim_lost" in entry.get("event", {})
        ]
        file_promoting = [
            index
            for index, entry in enumerate(file_entries)
            if entry.get("event", {}).get("role_changed")
            == {"from": "standby", "to": "promoting", "origin": "reclaim"}
        ]
        if len(file_observed) != 1 or len(file_named) != 1:
            failures.append(
                "the durable journal file carries "
                f"{len(file_observed)} field_claim_observed "
                f"records, {len(file_named)} naming the standing "
                "owner — the contract is exactly one"
            )
        elif file_lost and file_promoting and not (
            file_lost[0] < file_named[0] < file_promoting[0]
        ):
            failures.append(
                "the durable journal file orders the episode "
                "wrong — the field_claim_observed record must "
                "sit in seq order between the loss and the "
                "reclaim walk"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "durable",
                "observed": len(file_named),
                "ordered": True,
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        # Detach the claim attachments cleanly: release whatever
        # hold each still carries — a hold left standing keeps the
        # field claimed for a dead token — then close. The release
        # drops only the connection's own hold, so it never takes
        # the owner's claim down with it.
        for client in (verdict_io, induction_io, observed_io):
            if client is not None:
                try:
                    client.request({"op": "release_writer"})
                except Exception:
                    pass
                client.close()
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
        "shape — the pass must fail naming the attributed record",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = claim_observed_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "claim-observed: the doctored expectation wanted the "
                "observed claimant unrecorded — an inconclusive run "
                "offers the doctored case no evidence"
            )
            return 1
        eprint(f"claim-observed: inconclusive — {inconclusive}")
        print(f"claim-observed-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"claim-observed: {line}")
        return 1
    for failure in failures:
        eprint(f"claim-observed: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"claim-observed: the {args.tamper} case passed "
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
        f"claim-observed-digest {digest} — tracking by tick "
        f"{evidence['converged']}, one attributed "
        f"field_claim_observed naming "
        f"{evidence['observed_claimant']:#x} at seq "
        f"{evidence['observed_seq']} between the recorded loss and "
        f"the reclaim walk, launch roles restored at tick "
        f"{evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
