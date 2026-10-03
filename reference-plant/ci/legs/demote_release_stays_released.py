#!/usr/bin/env python3
"""The demote-release-stays-released leg for the reference plant —
the consumer-boundary mirror of the qa rig's
`a_voluntary_demote_hands_the_claim_to_the_fencing_armed_peer`
regression (#1270's contract), pinned on the manifest-declared
redundant pair and never before on the customer-owned deployment: a
**voluntary** demote's release of the write claim stays released
(WW-ENG-003, WW-LCM-001).

`POST /demote` is the deliberate hand-back: the demoted member's
release marks the standing claim yielded and drops the member from
its holder set, and the released claim is the *successors'* to take.
The orphan cycle's conditional re-arm exists for the opposite case —
an ownership the field took, so its released claim is exactly the
wedge the re-arm covers — and it is that re-arm, on a peer whose
ownership ended in its own requested demotion, that put a **live**
claim back under the demoted member's own token within about one
scan: a claim fencing every conditional successor while the member
that holds it reports `standby`. So the run asserts the shape the
`yielded` mark records — `demote_inner`'s request-origin release sets
it, decision 91's yielded hand-back is what it models — on the
shipped endpoints only:

- the declared pair, launched the way the manifest declares it —
  **unkeyed** on its declared wildcard binds, the shipped shape the
  finding reproduced on — converges with the field owner `active` and
  the declared standby `tracking`;
- the involuntary transfer first, because it is what arms the other
  half: `POST /promote` on the tracking standby while the owner still
  runs demotes the launch owner in place through its own fenced
  write, journals the attributed `field_claim_lost`, and re-joins the
  promoted peer's claim-declared monitor — the **fencing-loss-armed
  ex-owner** whose bound reclaim is the documented conditional path
  the released claim must resolve through;
- the voluntary demote: `POST /demote` on that new owner. The release
  lands synchronously with the request — the claim stays standing
  only as the holderless yielded hand-back, still naming the demoted
  member's token, with no live holder behind it;
- the hand-off window, watched scan by scan in the documented
  tracking-first order: the released claim **never** settles back
  under the demoted member's own token (the re-arm the `yielded`
  mark keeps off), and it resolves instead through the recorded
  conditional path — here the fencing-loss-armed ex-owner's bound
  reclaim, `reclaim_writer`'s `reclaim`-origin `standby → promoting
  → active` walk — reconverging the pair to exactly one
  field-owning `active` plus one `tracking` standby, the released
  member reporting `standby` throughout;
- the demoted member's own evidence: its orphan transition journals
  (`field_orphaned`) and **no** `field_claim_rearmed` record ever
  lands — the durable attribution #1270 added for a granted
  re-arm, which on this contract's pair must stay silent — beside
  the ex-owner's reclaim walk, both peers' manifest-declared
  durable journal files carrying the episode;
- the launch layout restored: the released claim came back through
  the recorded conditional path onto the **launch owner** itself, so
  the pair rests on its declared launch roles — the field owner
  `active`, the declared standby `tracking` it — with the claim under
  that owner's own token. The manifest-declared unkeyed duty member
  is exactly the member that cannot step down by hand (it declares no
  tracking source it could prove, so its `POST /demote` is refused
  `no_tracking_source`), which is why the launch roles are restored
  *by* the reclaim rather than by an operator switch back.

The contract postdates the released artifact sets — an artifact set
cut before it reads this way until a release carries it: where the
launched tooling answers no attributed fencing verdict or declares no
monitor, serves a checkpoint without the ownership stamps, refuses
the claim verbs, reports no sync vocabulary, journals no role origin,
or — the contract's own subject — leaves the released claim standing
under the demoted member's own token or lands its
`field_claim_rearmed` record, the run's evidence is the pre-contract
shape and the leg reports
`demote-release-stays-released-digest inconclusive` rather than
asserting, until the manifest repins a release carrying the contract.

Usage:

    demote_release_stays_released.py --plant-server PATH \
        --controller PATH --model model/plant.json \
        --dynamics model/dynamics.json --scenario ci/scenario.json \
        --manifest deploy/manifest.json

On success one `demote-release-stays-released-digest <sha256>` line
prints — the check runs two passes and compares them
(`demote-release-stays-released-nondeterministic`; the pair stage
forms the nondeterministic and unchecked names on the leg's file
stem, while the contract's own failed diagnostic is the declared
`demote-release-rearm-failed`). A contract violation reports
`demote-release-stays-released: …` lines on stderr and exits 1. The
doctored case `--tamper expect-rearm` flips the leg's own expectation
to the defect shape — asserting the demoted member's token still
holds the released claim — so the pass must fail naming the
resolution it observed instead, proving the release-stays-released
assertion fires on the honest hand-off rather than passing
unexercised.
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import claim_reclaim
import driver_recovery
import failover
import foreign_claim_release
import pair
import simulate
import stranded_rejoin


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored case.
# The doctored case: a leg asserting the demoted member's own token
# still holds the released claim — the re-arm the `yielded` mark
# keeps off — must surface the named diagnostic on the honest
# hand-off rather than passing an unexercised contract.
LEG = {
    # The next free slot after the claim-class legs origin/main added
    # (claim-skew-bound 730, pending-source-pull 740) — the stage runs
    # the legs in this order and no two may share one.
    "order": 750,
    "title": "the demote-release-stays-released leg",
    "passes": "demote-release-stays-released-leg",
    "failed": "demote-release-rearm-failed",
    "tampers": [
        {
            "name": "expect-rearm",
            "passed": "an expect-rearm case passed the demote-release leg",
            "missed": "the expect-rearm case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the demoted member's own token to hold the released claim",
            ],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The inconclusive classification the sibling legs raise — this leg
# stages its involuntary transfer through `stranded_rejoin`'s re-join
# cycle, so both classes surface through the pass. The first arg is
# the stable reason the `inconclusive` digest line prints — two
# identical passes must share it — and the optional second arg the
# run's own evidence, reported on stderr only, where minted owner
# tokens and ephemeral endpoints belong.
Inconclusive = foreign_claim_release.Inconclusive


# The driven-scan bounds the episode's phases run: the fenced
# demote-in-place settles inside a couple of scans, and the released
# claim's resolution through the recorded conditional path is the
# bounded window the contract names — the defect's indefinite
# standing claim under the demoted member's own token being the
# pre-contract shape the bound catches — alongside the settle train
# that proves the reconverged pair identical before its launch roles
# are read.
HANDOFF_SCANS = 12
SETTLE_TICKS = 4

# The probe attachment's own owner token — used only by the contract
# gate's `reclaim_writer` ask, which must refuse the launch owner's
# live claim it shares no owner with. A fresh plant per leg means the
# value never collides with a controller's per-process minted token.
PROBE_CLAIM = 0xF0DD


# The doctored expectation's stable evidence prefix — every failure
# the tampered pass records carries it, so the check's negative case
# finds it whether the honest run resolved the release or a predating
# release offered the case no evidence at all.
TAMPER_EVIDENCE = (
    "the doctored expectation wanted the demoted member's own token to "
    "hold the released claim"
)


def claim_state(probe_io):
    """The field's claim state as one read-only word — `unclaimed`
    while no claim stands, `held` while another owner's claim stands
    (the caller reads the naming owner off the same answer), or the
    raw verdict's word where the release predates the claim-state
    probe. `probe_writer` touches no holder set, so reading the
    field's ownership can never move it."""
    probe = probe_io.request({"op": "probe_writer"})
    if claim_reclaim.unsupported_verb(probe):
        return "unsupported", probe
    if failover.probe_kind(probe) == "unclaimed":
        return "unclaimed", probe
    owner = claim_reclaim.verdict_owner(probe)
    if owner is None:
        return "unnamed", probe
    return "held", probe


def gate_contract_surface(rig, verdict_io, probe_io, a_token, a_url,
                          failures, evidence):
    """The contract surface the episode asserts on: the standing claim
    fencing third-party mutations while naming the launch owner and
    its declared dialable monitor, the `reclaim_writer` verb answered
    (the gate's own probe reclaim refusing the live claim it shares no
    owner with), the served checkpoint's ownership stamps, and the
    durable journal files the manifest declares. Each absence is the
    release predating the substrate, never a violation of it."""
    if rig.duty_files.get("journal_file") is None or (
        rig.standby_files.get("journal_file") is None
    ):
        raise Inconclusive(
            "the manifest's pair declares no journal files — the "
            "durable half of the release audit is absent"
        )
    probe0 = verdict_io.request({"op": "step", "dt": 0})
    evidence["probe0"] = probe0
    if not claim_reclaim.mutation_fenced(probe0):
        failures.append(
            "the field held no writer claim after convergence — a "
            f"third-party probe answered {probe0}, so the leg has no "
            "standing claim to release"
        )
        raise Abort
    if claim_reclaim.verdict_owner(probe0) is None:
        raise Inconclusive(
            "the fencing verdict names no standing owner — the "
            "pinned release predates the verdict attribution the "
            "release's claimants read",
            f"the standing fencing verdict was {probe0}",
        )
    if claim_reclaim.verdict_owner(probe0) != a_token:
        failures.append(
            "the standing claim names a foreign token, not the launch "
            f"owner — the pair is not in its launch claim state: "
            f"{probe0}"
        )
        raise Abort
    declared0 = stranded_rejoin.verdict_monitor(probe0)
    if declared0 is None:
        raise Inconclusive(
            "the standing claim declares no monitor — the pinned "
            "release predates the claim-declared monitor the "
            "voluntary demote's successors resolve",
            f"the standing fencing verdict was {probe0}",
        )
    if foreign_claim_release.monitor_kind(
        declared0, foreign_claim_release.monitor_addr(a_url)
    ) != "owner":
        raise Inconclusive(
            "the standing claim's declared monitor is not the field "
            "owner's own dialable monitor — the pinned release "
            "predates the claim-monitor normalization",
            f"the declared monitor was {declared0}",
        )
    doc = pair.get(f"{a_url}/checkpoint", "GET /checkpoint", failures)
    evidence["doc0"] = doc
    if "source_owns_field" not in doc or "line_owner" not in doc:
        raise Inconclusive(
            "the served checkpoint carries no field-ownership stamps "
            "— the pinned release predates the field-arbitrated "
            "monitor contract the orphan verdict rides on",
            f"the checkpoint serves {sorted(doc)}",
        )
    gate = probe_io.request({"op": "reclaim_writer", "owner": PROBE_CLAIM})
    evidence["reclaim_gate"] = gate
    if claim_reclaim.unsupported_verb(gate):
        raise Inconclusive(
            "reclaim_writer answered invalid_request — the pinned "
            "release predates the bound re-grant verb the released "
            "claim's conditional path resolves through",
            f"reclaim_writer answered {gate}",
        )
    if gate.get("result") in ("done", "claimed_shared"):
        failures.append(
            "a foreign reclaim grant took the live claim — the "
            "never-preempts-a-live-holder rule the demoted ex-owner's "
            f"bound reclaim depends on is broken: {gate}"
        )
        raise Abort
    if not claim_reclaim.mutation_fenced(gate):
        raise Inconclusive(
            "reclaim_writer answered neither a grant nor a fencing "
            "refusal — the pinned release predates the grant's "
            "contract shape",
            f"reclaim_writer answered {gate}",
        )


def handoff_window(a_url, b_url, probe_io, armed_token, demoted_token,
                   failures, evidence):
    """The hand-off window: the driven pair tick train the released
    claim's resolution runs across — the voluntarily demoted member
    scanned first, its orphaned pulls the window the `yielded` mark
    keeps the re-arm off, then the fencing-loss-armed ex-owner whose
    bound reclaim is the documented conditional path. Watches, scan
    by scan, that the released claim never settles back under the
    demoted member's own token and that the pair walks exactly one
    field-owning `active` plus one `tracking` standby. Returns
    `(window, settled)` — `window` the per-scan observations for the
    digest, `settled` the reclaiming ex-owner's `active` role report
    and the released member's tracking one, or None while the claim
    stands released. The per-scan `claim` word is the verdict's own
    attribution in the leg's vocabulary — `unclaimed`, `demoted`
    (still naming the released member's own token), `successor` (the
    fencing-loss-armed ex-owner holds it) or `foreign`."""
    window, settled = [], None
    for _ in range(HANDOFF_SCANS):
        pair.scan(b_url, failures)
        pair.scan(a_url, failures)
        word, probe = claim_state(probe_io)
        if word == "unsupported":
            raise Inconclusive(
                "probe_writer answered invalid_request — the pinned "
                "release predates the claim-state probe the released "
                "claim's window is read through",
                f"probe_writer answered {probe}",
            )
        if word == "unnamed":
            raise Inconclusive(
                "the claim-state observation names no owner — the "
                "pinned release predates the verdict attribution the "
                "released claim's audit reads",
                f"probe_writer answered {probe}",
            )
        a_role = pair.get(f"{a_url}/role", "GET /role", failures)
        b_role = pair.get(f"{b_url}/role", "GET /role", failures)
        owner = claim_reclaim.verdict_owner(probe)
        if word == "held":
            word = (
                "demoted"
                if owner == demoted_token
                else "successor"
                if owner == armed_token
                else "foreign"
            )
        window.append(
            {
                "claim": word,
                "armed": (
                    a_role.get("role")
                    + "/"
                    + stranded_rejoin.sync_kind(a_role)
                ),
                "demoted": b_role.get("role")
                + "/"
                + stranded_rejoin.sync_kind(b_role),
            }
        )
        # The released member must never take the field back: a
        # re-armed claim under its own token leaves it `standby`, and
        # any promotion of its own is that same defect read from the
        # role surface.
        if b_role.get("role") != "standby":
            failures.append(
                "the voluntarily demoted member left standby for "
                f"{b_role.get('role')!r} — a member reporting standby "
                "after a deliberate release holds no claim of its own "
                "and claims no promotion"
            )
            raise Abort
        # The ex-owner may only walk back toward active: the
        # reclaim's `promoting` on the granted claim and its `active`
        # on the next field-owning scan.
        if a_role.get("role") not in ("standby", "promoting", "active"):
            failures.append(
                "the fencing-loss-armed ex-owner reported "
                f"{a_role.get('role')!r} resolving the released claim "
                "— its bound reclaim may only walk back toward active"
            )
            raise Abort
        if word == "foreign":
            failures.append(
                "the released claim came to rest under a claim "
                f"neither declared member holds: {probe}"
            )
            raise Abort
        if (
            word == "successor"
            and a_role.get("role") == "active"
            and claim_reclaim.tracking(b_role)
        ):
            settled = (a_role, b_role)
            break
    evidence["window"] = window
    return window, settled


def demote_release_stays_released_pass(args, tamper):
    """The demote-release-stays-released run: launch the unkeyed
    declared-binds pair, converge, gate the contract surface, stage
    the involuntary transfer that arms the ex-owner, take the
    voluntary demote's released claim through its hand-off window to
    the documented conditional path, and restore the launch layout.
    Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "demote-release-stays-released leg has nothing to "
            "exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    rig = verdict_io = probe_io = None
    a_url = b_url = None
    try:
        rig = foreign_claim_release.launch_unkeyed(args, declared)
        a_url, b_url = rig.duty_url, rig.standby_url
        # The claim attachments the episode reads through: the rig's
        # own client stays read-only, `verdict_io` runs the
        # third-party mutation probes whose fencing verdicts carry the
        # standing claim's declared monitor, and `probe_io` runs the
        # read-only claim-state observations and the gate's foreign
        # reclaim ask. Neither ever holds the field's claim.
        verdict_io = simulate.PlantClient(rig.plant_addr)
        probe_io = simulate.PlantClient(rig.plant_addr)

        # Phase 1 — the declared wildcard binds deployed verbatim: the
        # unkeyed deployment shape the released-claim finding
        # reproduced on, where the standing claim's declared monitor is
        # the only tracking source a demoted peer can prove.
        binds = []
        for name, entry, bound in (
            (duty_decl["name"], duty_decl, rig.duty_bound),
            (standby_decl["name"], standby_decl, rig.standby_bound),
        ):
            declared_host = entry.get("listen", "").rsplit(":", 1)[0]
            bound_host = bound[0].rsplit(":", 1)[0] if bound else None
            binds.append(declared_host)
            if bound_host != declared_host:
                failures.append(
                    f"{name} did not bind its declared listen host "
                    f"{declared_host} — the spawn reported "
                    f"{bound or 'no listener'}"
                )
        if failures:
            raise Abort

        # Phase 2 — convergence and the contract surface: the field
        # owner `active` with the declared standby `tracking` it, the
        # launched owner's own claim token (the attribution every
        # verdict below is read against), and the substrate the
        # episode's own evidence reads.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        a_token = failover.owner_token(rig.duty_preamble)
        if a_token is None:
            raise Inconclusive(
                "the launched field owner recorded no claim line — the "
                "pinned release claims only on promotion, predating "
                "the claim lifecycle the released claim rides on"
            )
        evidence["armed_token"] = a_token
        gate_contract_surface(
            rig, verdict_io, probe_io, a_token, a_url, failures, evidence
        )
        digest_entries.append(
            {
                "phase": "converge",
                "binds": binds,
                "duty_role": converged["duty_role"].get("role"),
                "standby_role": converged["standby_role"].get("role"),
                "probe": "fenced",
                "owner": "named",
                "monitor": "owner",
                "reclaim": "fenced",
                "stamps": "present",
            }
        )

        # Phase 3 — the involuntary transfer, which is what arms the
        # other half of the contract: `POST /promote` on the tracking
        # standby while the launch owner still owns demotes it in
        # place through its own fenced write, journals the attributed
        # `field_claim_lost`, and re-joins the promoted peer's
        # claim-declared monitor — the fencing-loss-armed ex-owner
        # whose bound reclaim the voluntary release hands the field
        # to.
        transfer = stranded_rejoin.rejoin_cycle(
            rig,
            a_url,
            b_url,
            rig.duty_files,
            True,
            verdict_io,
            failures,
            evidence,
            "transfer",
        )
        digest_entries.append({"phase": "transfer", **transfer})
        if failures:
            raise Abort
        b_token = claim_reclaim.verdict_owner(
            evidence["transfer_verdict"]
        )
        if b_token is None or b_token == a_token:
            raise Inconclusive(
                "the promoted peer's claim token was not learned from "
                "the fencing verdict — the pinned release predates "
                "the verdict attribution the demoted member's own "
                "token is read against",
                f"the transfer's fencing verdict was "
                f"{evidence['transfer_verdict']}",
            )
        evidence["demoted_token"] = b_token
        promoted_role = pair.get(f"{b_url}/role", "GET /role", failures)
        if promoted_role.get("role") != "active":
            failures.append(
                "the promoted peer does not serve active before the "
                f"voluntary demote — the release under test never ran: "
                f"{promoted_role}"
            )
            raise Abort

        # The journal floors the released-claim episode must be read
        # above — taken once, before the demotion.
        floors = {
            "armed": len(
                pair.get(f"{a_url}/journal", "GET /journal", failures)
            ),
            "demoted": len(
                pair.get(f"{b_url}/journal", "GET /journal", failures)
            ),
        }

        # Phase 4 — the voluntary demote: `POST /demote` on the new
        # owner. The release lands with the request — the gate closes,
        # the claim's holder set drops this member, and the standing
        # claim is marked yielded. What remains is the holderless
        # hand-back: a claim that still names the demoted member's
        # token, with no live holder behind it, and the successors'
        # conditional paths to take it.
        demote_status, demote_report = pair.request(f"{b_url}/demote", {})
        evidence["demote"] = {"status": demote_status, "body": demote_report}
        if demote_status != 200 or demote_report.get("role") != "demoting":
            failures.append(
                "POST /demote on the promoted peer answered "
                f"{demote_status} {demote_report} — the voluntary "
                "demotion must answer a demoting report"
            )
            raise Abort
        released, release_probe = claim_state(probe_io)
        evidence["released_probe"] = release_probe
        if released == "unsupported":
            raise Inconclusive(
                "probe_writer answered invalid_request — the pinned "
                "release predates the claim-state probe the released "
                "claim is read through",
                f"probe_writer answered {release_probe}",
            )
        if released == "unnamed":
            raise Inconclusive(
                "the claim-state observation names no owner — the "
                "pinned release predates the verdict attribution the "
                "released claim's audit reads",
                f"probe_writer answered {release_probe}",
            )
        # The released claim's own shape: the holderless hand-back
        # still naming the demoted member's own token is the demotion
        # shape #1270's fix keeps released; a dropped claim or one that
        # already names another owner is a release the contract also
        # allows — the window below resolves either way.
        released = (
            "dropped"
            if released == "unclaimed"
            else "holderless"
            if claim_reclaim.verdict_owner(release_probe) == b_token
            else "foreign"
        )
        evidence["released"] = released
        digest_entries.append(
            {"phase": "demote", "report": demote_report.get("role"),
             "released": released}
        )

        # Phase 5 — the hand-off window: the released claim must never
        # settle back under the demoted member's own token, and it
        # must resolve through the recorded conditional path.
        window, settled = handoff_window(
            a_url, b_url, probe_io, a_token, b_token, failures, evidence
        )
        evidence["handed"] = [
            entry["claim"] for entry in window if entry["claim"] == "demoted"
        ]
        evidence["resolved_scan"] = next(
            (
                number
                for number, entry in enumerate(window, 1)
                if entry["claim"] == "successor"
            ),
            None,
        )
        if tamper == "expect-rearm":
            # The doctored expectation: the released claim still held
            # under the demoted member's own token. The honest window
            # released it onto the fencing-loss-armed ex-owner, so the
            # assertion cannot stand.
            failures.append(
                f"{TAMPER_EVIDENCE} — the honest window observed "
                f"{window} and released the claim onto the "
                "fencing-loss-armed ex-owner, the demoted member's own "
                "token never keeping it"
            )
            raise Abort
        if settled is None:
            if window and window[-1]["claim"] == "demoted":
                raise Inconclusive(
                    "the pinned release predates the demote-release "
                    "contract — the demoted member's orphan-cycle "
                    "ensure re-armed the claim its own voluntary "
                    "demotion released, under its own token",
                    f"the window was {window}",
                )
            failures.append(
                "the released claim never resolved through a "
                "conditional path — the field stood unclaimed beneath "
                f"the released holderless claim across the window "
                f"{window}"
            )
            raise Abort
        digest_entries.append(
            {"phase": "window", "window": window, "resolved": "successor"}
        )

        # Phase 5's tail — the served evidence of both members: the
        # demoted member's orphan transition journals (the release
        # left it following an ownerless line) and **no**
        # `field_claim_rearmed` record ever lands on it, the
        # attribution #1270 added for a granted re-arm. The ex-owner
        # answers the reclaimed field with the `reclaim`-origin walk
        # its bound grant takes, never an operator's promote, and
        # suffers no second fencing loss.
        b_added = pair.get(
            f"{b_url}/journal", "GET /journal", failures
        )[floors["demoted"]:]
        a_added = pair.get(
            f"{a_url}/journal", "GET /journal", failures
        )[floors["armed"]:]
        evidence["demoted_added"] = b_added
        b_kinds = {
            kind for entry in b_added for kind in entry.get("event", {})
        }
        a_kinds = {
            kind for entry in a_added for kind in entry.get("event", {})
        }
        evidence["demoted_kinds"] = sorted(b_kinds)
        evidence["armed_kinds"] = sorted(a_kinds)
        if "field_orphaned" not in b_kinds:
            raise Inconclusive(
                "the demoted member's orphaned transition left no "
                "field_orphaned record — the pinned release predates "
                "the journaled orphan contract the released claim's "
                "window runs on",
                f"its added kinds were {sorted(b_kinds)}",
            )
        if "field_claim_rearmed" in b_kinds:
            raise Inconclusive(
                "the pinned release predates the demote-release "
                "contract — the demoted member journaled a "
                "field_claim_rearmed record for the claim its own "
                "voluntary demotion released",
                f"its added kinds were {sorted(b_kinds)}",
            )
        walk_a = stranded_rejoin.role_walk(a_added)
        evidence["armed_walk"] = walk_a
        if any(origin is None for _from, _to, origin in walk_a):
            raise Inconclusive(
                "the ex-owner's role walk carries no switch origin — "
                "the pinned release predates the attribution fields "
                "the reclaim's own record reads",
                f"the role walk was {walk_a}",
            )
        if ("standby", "promoting", "reclaim") not in walk_a or (
            "promoting", "active", "reclaim"
        ) not in walk_a:
            failures.append(
                "the released claim's resolution left no "
                "reclaim-origin walk back to active on the "
                f"fencing-loss-armed ex-owner — an operator or restart "
                f"path ran instead: {walk_a}"
            )
            raise Abort
        if stranded_rejoin.lost_entries(b_added):
            failures.append(
                "the demoted member journaled a fencing loss it never "
                "suffered — its voluntary release is not a loss"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "served",
                "demoted": sorted(b_kinds),
                "armed": sorted(a_kinds),
                "walk": "reclaim",
                "rearm": "silent",
                "losses": "settled",
            }
        )

        # Phase 6 — the reconvergence: exactly one field-owning
        # `active` — the reclaiming ex-owner — with the released
        # member tracking it, the claim standing under the ex-owner's
        # own token and declaring its own dialable monitor, the served
        # documents honestly stamped, and the pair identical across a
        # tracking-first settle train.
        if settled is None or settled[0].get("role") != "active" or not (
            claim_reclaim.tracking(settled[1])
        ):
            failures.append(
                "the pair did not reconverge to one field-owning "
                "active plus one tracking standby — the released "
                f"claim's resolution left {settled}"
            )
            raise Abort
        handover = []
        for _ in range(SETTLE_TICKS):
            _tracked, owner_snapshot = rig.tick(
                b_url, a_url, failures,
                diverged="the reconverged pair's images diverged at "
                "tick {tick} — the released claim's resolution was "
                "not bumpless",
            )
            handover.append(owner_snapshot["tick"])
        a_role = pair.get(f"{a_url}/role", "GET /role", failures)
        b_role = pair.get(f"{b_url}/role", "GET /role", failures)
        if a_role.get("role") != "active":
            failures.append(
                f"the reclaiming ex-owner does not serve active after "
                f"the settle train: {a_role}"
            )
            raise Abort
        if not claim_reclaim.tracking(b_role):
            failures.append(
                "the released member did not settle a tracking "
                f"standby on the reclaiming ex-owner: {b_role}"
            )
            raise Abort
        seated, seat_probe = claim_state(probe_io)
        evidence["seated"] = seated
        if claim_reclaim.verdict_owner(seat_probe) != a_token:
            failures.append(
                "the re-seated claim does not stand under the "
                f"reclaiming ex-owner's own token: {seat_probe}"
            )
            raise Abort
        declared_seated = stranded_rejoin.verdict_monitor(seat_probe)
        if declared_seated is None:
            raise Inconclusive(
                "the re-seated claim declares no monitor — the pinned "
                "release predates the claim-declared monitor the "
                "released member's re-join resolves",
                f"the claim-state probe answered {seat_probe}",
            )
        if foreign_claim_release.monitor_kind(
            declared_seated, foreign_claim_release.monitor_addr(a_url)
        ) != "owner":
            failures.append(
                "the re-seated claim declares monitor "
                f"{declared_seated} — not the reclaiming ex-owner's "
                f"dialable monitor "
                f"{foreign_claim_release.monitor_addr(a_url)}"
            )
            raise Abort
        fenced = verdict_io.request({"op": "step", "dt": 0})
        evidence["seated_probe"] = fenced
        if not claim_reclaim.mutation_fenced(fenced) or (
            claim_reclaim.verdict_owner(fenced) != a_token
        ):
            failures.append(
                "the re-seated claim does not fence third-party "
                f"mutations under the ex-owner's own token: {fenced}"
            )
            raise Abort
        doc_a = pair.get(f"{a_url}/checkpoint", "GET /checkpoint", failures)
        doc_b = pair.get(f"{b_url}/checkpoint", "GET /checkpoint", failures)
        if doc_a.get("source_owns_field") is not True:
            failures.append(
                "the reclaiming ex-owner serves a checkpoint whose "
                f"source_owns_field is {doc_a.get('source_owns_field')} "
                "— the field owner's honest stamp is ownership"
            )
            raise Abort
        if doc_b.get("source_owns_field") is not False:
            failures.append(
                "the released member serves a checkpoint whose "
                "source_owns_field is "
                f"{doc_b.get('source_owns_field')} — a tracking peer's "
                "honest stamp is non-ownership"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "converge-back",
                "settled": handover,
                "armed": a_role.get("role"),
                "demoted": b_role.get("role") + "/" + (
                    "tracking" if claim_reclaim.tracking(b_role) else "other"
                ),
                "claim": "ex-owner",
                "monitor": "owner",
                "fenced": "third-party",
            }
        )
        evidence["settled"] = handover[-1]

        # Phase 7 — the durable mirror: both peers' manifest-declared
        # journal files carry the episode, not only their served
        # tails. The ex-owner's file holds the attributed loss its
        # involuntary transfer journaled, the adoption that resolved
        # its re-join, its orphaned pull, and the reclaim-origin walk
        # back to the field; the released member's file holds the
        # request-origin demote walk and its own orphaned pull — and
        # still no `field_claim_rearmed` record, the durable
        # counterpart of the served audit above.
        durable = {}
        for url, name, files in (
            (a_url, duty_decl["name"], rig.duty_files),
            (b_url, standby_decl["name"], rig.standby_files),
        ):
            # A served journal read is the durability-attesting one —
            # it waits the sink's drain out — so the declared file
            # read right after it is caught up through the run's own
            # recorded tail rather than a scan behind it.
            evidence["tail_" + name] = len(
                pair.get(f"{url}/journal", "GET /journal", failures)
            )
            path = files["journal_file"]
            kinds = stranded_rejoin.durable_kinds(path)
            durable[name] = sorted(kinds)
            evidence["durable_" + name] = sorted(kinds)
            if "role_changed" not in kinds:
                raise Inconclusive(
                    f"{name}'s durable journal file carries no "
                    "role_changed record — the pinned release predates "
                    "the journaled role record the episode's durable "
                    "mirror reads",
                    f"its durable kinds were {sorted(kinds)}",
                )
        for owed in (
            "field_claim_lost",
            "tracking_source_adopted",
            "field_orphaned",
            "role_changed",
        ):
            if owed not in durable[duty_decl["name"]]:
                failures.append(
                    f"{duty_decl['name']}'s durable journal file carries "
                    f"no {owed} record — the episode's own durable "
                    "mirror is incomplete"
                )
        for owed in ("field_orphaned", "role_changed"):
            if owed not in durable[standby_decl["name"]]:
                failures.append(
                    f"{standby_decl['name']}'s durable journal file "
                    f"carries no {owed} record — the episode's own "
                    "durable mirror is incomplete"
                )
        if "field_claim_rearmed" in durable[standby_decl["name"]]:
            raise Inconclusive(
                "the pinned release predates the demote-release "
                "contract — the demoted member's durable journal "
                "journaled a field_claim_rearmed record for the claim "
                "its own voluntary demotion released",
                f"its durable kinds were "
                f"{durable[standby_decl['name']]}",
            )
        a_durable = stranded_rejoin.journal_entries(
            rig.duty_files["journal_file"]
        )
        a_durable_losses = stranded_rejoin.lost_entries(a_durable)
        evidence["durable_losses"] = a_durable_losses
        if len(a_durable_losses) != 1 or (
            a_durable_losses[0][1].get("claimant") != b_token
        ):
            failures.append(
                f"{duty_decl['name']}'s durable journal carries the "
                f"losses {a_durable_losses} — expected exactly one "
                "field_claim_lost attributed to the promoted peer's "
                "own token"
            )
        durable_walk = stranded_rejoin.role_walk(a_durable)
        if ("standby", "promoting", "reclaim") not in durable_walk or (
            "promoting", "active", "reclaim"
        ) not in durable_walk:
            failures.append(
                f"{duty_decl['name']}'s durable journal carries the "
                f"role walk {durable_walk} — the reclaim-origin walk "
                "back onto the field went unrecorded"
            )
        b_durable_walk = stranded_rejoin.role_walk(
            stranded_rejoin.journal_entries(
                rig.standby_files["journal_file"]
            )
        )
        evidence["durable_demoted_walk"] = b_durable_walk
        # The voluntary demote's own segment of the walk — the
        # `active → demoting` transition and the settle behind it,
        # both under the request origin, read after the promotion
        # walk the involuntary transfer recorded.
        voluntary = []
        for walk in b_durable_walk:
            if walk[:2] == ("active", "demoting"):
                voluntary = [walk]
            elif voluntary:
                voluntary.append(walk)
        if voluntary != [
            ("active", "demoting", "request"),
            ("demoting", "standby", "request"),
        ]:
            failures.append(
                f"{standby_decl['name']}'s durable journal carries the "
                f"voluntary demote walk {voluntary} — expected exactly "
                "the request-origin active → demoting → standby"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "durable",
                "duty": durable[duty_decl["name"]],
                "standby": durable[standby_decl["name"]],
                "rearm": "silent",
            }
        )

        # Phase 8 — the launch layout restored: the released claim
        # came back through the recorded conditional path onto the
        # launch owner, so the pair rests on its launch roles with the
        # claim under that owner's own token. The manifest-declared
        # unkeyed duty member is exactly the member that cannot step
        # down by hand — it declares no tracking source it could
        # prove — so the launch roles are restored *by* the reclaim
        # rather than by an operator switch back, and the leg asserts
        # the restored layout where it stands.
        if not driver_recovery.roles_hold(
            rig, failures, "after the released claim resolved"
        ):
            raise Abort
        reseated, reseat_probe = claim_state(probe_io)
        evidence["reseated"] = reseated
        if claim_reclaim.verdict_owner(reseat_probe) != a_token:
            failures.append(
                "the restored claim does not stand under the launch "
                f"owner's own token: {reseat_probe}"
            )
            raise Abort
        final = pair.get(f"{a_url}/role", "GET /role", failures)
        digest_entries.append(
            {
                "phase": "restore",
                "ticks": handover,
                "duty": final.get("role"),
                "standby": "tracking",
                "claim": "launch-owner",
            }
        )
        evidence["final_tick"] = final.get("tick")
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except (Inconclusive, stranded_rejoin.Inconclusive):
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        for client in (verdict_io, probe_io):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
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
        choices=["expect-rearm"],
        help="doctor the leg's expectation to the pre-contract shape "
        "— asserting the demoted member's own token still holds the "
        "claim its voluntary demotion released — so the pass must "
        "fail naming the resolution it observed instead",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = (
            demote_release_stays_released_pass(args, args.tamper)
        )
    except (Inconclusive, stranded_rejoin.Inconclusive) as inconclusive:
        if args.tamper is not None:
            eprint(
                "demote-release-stays-released: "
                f"{TAMPER_EVIDENCE} — an inconclusive run offers the "
                "doctored case no evidence"
            )
            return 1
        # The digest line carries only the stable reason — the run's
        # own verdicts, tokens, and endpoints report on stderr, where
        # two identical passes need not share them.
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"demote-release-stays-released: inconclusive — {detail}")
        print(
            f"demote-release-stays-released-digest inconclusive — "
            f"{reason}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"demote-release-stays-released: {line}")
        return 1
    for failure in failures:
        eprint(f"demote-release-stays-released: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"demote-release-stays-released: the {args.tamper} case "
                "passed silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"demote-release-stays-released-digest {digest} — the "
        "manifest-declared unkeyed pair converged, the promoted "
        f"owner's voluntary demote released its claim "
        f"({evidence['released']}), and the hand-off window resolved it "
        f"onto the fencing-loss-armed ex-owner on scan "
        f"{evidence['resolved_scan']} of {len(evidence['window'])} "
        "with no observation naming the demoted member's own token — no "
        "field_claim_rearmed record landed in either journal — the pair "
        "reconverged to one active plus a tracking standby, and the "
        "launch roles stand with the claim under the launch owner at "
        f"tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
