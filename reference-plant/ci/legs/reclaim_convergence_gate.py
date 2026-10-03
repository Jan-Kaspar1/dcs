#!/usr/bin/env python3
"""The reclaim-convergence-gate leg for the reference plant — the
consumer-boundary mirror of the rig's reclaim-convergence-gate leg
(WW-ENG-003, WW-LCM-001 — the #1317 contract, decision 105, pinned on
the manifest-declared unkeyed pair): a fenced ex-owner carrying no
convergence evidence must never preempt a different owner's standing
claim — a merely transport-frozen incumbent's holderless claim reads
identical to a dead owner's at the arbitration, so the bound
`reclaim_writer` ask may issue only where the field just answered
`unclaimed` or the peer's convergence proof still stands — while a
converged ex-owner's bound reclaim of a released claim still takes
the field. A customer deployment's unkeyed seats are exactly where a
stale reclaimer must be refused: an unconverged peer adopting state
older than the incumbent's would silently roll its ownership epoch
back.

The claim-reclaim leg proves the released-preemption lifecycle and
the holderless-claim-recovery leg the dead-owner wedge's recovery;
this leg pins the race the finding records — the reclaim armed by the
fencing loss meeting the incumbent's merely-frozen transport:

- the manifest-declared pair launched unkeyed on its declared
  wildcard binds converges `active`/`tracking` — the born-active
  launch owner's recorded claim fencing third-party mutations while
  naming its token and its declared dialable monitor;
- a dedicated plant-socket attachment's monitor-less `claim_writer`
  — `controller: false`, a field tool's hold — preempts the launch
  owner: its first fenced write demotes it in place with a
  `field_claim_lost` attributed to the foreign token — the born-active
  seat fenced to `standby`/`unsynchronized`, the reclaim armed with
  no convergence evidence behind it;
- the sibling goes `orphaned` on the demoted peer's ownerless
  checkpoints, and `POST /promote` lands its conditional claim over
  the tool's hold — the promoted seat is the incumbent, its
  controller claim standing on its live line under its own token;
- the field freezes — `SIGSTOP` on the spawned plant stand-in, the
  `docker pause` shape — and the incumbent's bounded scan drops its
  link; paused itself (`SIGSTOP` on the incumbent's process) before
  the thaw, the incumbent cannot reattach while its claim stands
  holderless — the exact window the finding reproduces, the armed
  reclaim racing the reattach on the thawed field;
- through the window the fenced ex-owner's driven scans must keep
  reporting `standby`/`unsynchronized` — no convergence evidence
  ever lands — the fencing verdict keeps naming the incumbent's
  token, the field records no write, and neither peer's journal
  shows an ownership move; the incumbent's resumed scans re-attach
  on its live line and keep the ownership epoch;
- the positive control: once the ex-owner adopts the incumbent's
  claim-declared monitor and converges `tracking`, the incumbent's
  deliberate `POST /demote` leaves its claim standing yielded and
  holderless — and the converged fenced ex-owner's bound reclaim
  preempts it, the unattended `standby → promoting → active` walk
  journaled under the `reclaim` origin;
- the run ends with the pair back on its launch roles — the launch
  owner `active`, the declared standby `tracking` — both durable
  journal files carrying the episode's records.

The `claim_writer`/`release_writer`/`probe_writer` staging rides
dedicated attachments on the same plant-socket protocol the shipped
`dcs-plant-ctl` speaks, while the tool itself drives the window's
third-party fencing probes.

The contract postdates the pinned release line: where the launched
tooling predates it — no owner-token claim line, an unanswered claim
verb, a fencing verdict naming no owner or no declared monitor, the
wildcard bind stored verbatim, a checkpoint without the ownership
stamps, a standby report carrying no sync vocabulary, a loss or
adoption unjournaled, a scan the frozen field stalls past its
declared bound, or the unsynchronized reclaimer simply taking the
standing claim — the run's own evidence is the pre-contract shape
and the leg reports `reclaim-convergence-gate-digest inconclusive`
rather than asserting until the manifest repins a release carrying
the contract.

Usage:

    reclaim_convergence_gate.py --plant-server PATH --controller PATH \
        --plant-ctl PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `reclaim-convergence-gate-digest <sha256>` line prints —
the check runs two passes and compares them
(`reclaim-convergence-gate-nondeterministic`). A contract violation
reports `reclaim-convergence-gate: …` lines on stderr and exits 1 —
the check's `reclaim-convergence-failed`. `--tamper opened-field`
doctors the frozen-field window open — the incumbent's holderless
claim preempted and released under a scratch token — so the
unsynchronized reclaimer's legal unclaimed-field re-grant takes the
field and the leg's gate assertions must report the named diagnostic
rather than passing an unexercised contract.
"""

import argparse
import json
import os
import signal
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import bounded_liveness
import claim_reclaim
import driver_recovery
import failover
import foreign_claim_release
import ownerless_backoff
import pair
import simulate
import stranded_rejoin


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: the frozen-field window doctored open so the
# unsynchronized reclaimer takes the field — the leg asserting the
# incumbent's claim stood must surface the named diagnostic rather
# than passing an unexercised contract.
LEG = {
    "order": 770,
    "title": "the reclaim-convergence-gate leg",
    "passes": "reclaim-convergence",
    "failed": "reclaim-convergence-failed",
    "tools": {
        "plant-ctl": "dcs-plant-ctl",
    },
    "tampers": [
        {
            "name": "opened-field",
            "passed": "an opened-field case passed the reclaim-convergence-gate leg",
            "missed": "the opened-field case did not report its named diagnostic",
            "evidence": [
                "the unsynchronized reclaim took the field"
            ],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort
Inconclusive = foreign_claim_release.Inconclusive


# The driven-scan bounds the episode's phases run: the fenced demote
# and the orphaned verdict settle inside a couple of scans, the race
# window spans a handful of driven scans on the armed ex-owner, and
# the bounded rejoin/reclaim windows mirror the sibling legs'. The
# incumbent's frozen scan owes the reattach-window's cost — the
# driver's one request timeout per backoff window, ~5s under the
# released tooling — so twenty seconds is far past every healthy
# answer and far short of the defect's once-per-point stall.
WATCH_SCANS = 6
ORPHAN_SCANS = 6
RACE_SCANS = 6
RESOLVE_SCANS = 16
FROZEN_SCANS = 2
SCAN_BOUND_S = 20.0

# The wall-clock pacings the levers need: the armed reattach backoff
# each thawed contact waits out, the plant's reaped-holder margin
# once it resumes, and the spacing the failed claim-monitor adoption
# the frozen incumbent owes spends — `ANNOUNCED_VERIFY_RETRY`'s 4s
# window — before the ex-owner's next verify lands. The paced
# `RESOLVE_SCANS` rejoin budget outlives the window the last failed
# verify spent.
REATTACH_WAIT = 1.4
REAP_MARGIN_S = 0.4
REJOIN_SPACING_S = 0.6
PING_BOUND_S = 2.0

# The dedicated attachments' owner tokens — the monitor-less tool
# claim the fenced demotion stages, the gate probe's foreign token,
# and the `opened-field` tamper's scratch claim — small fixed values
# that cannot collide with a controller's per-process minted token.
FOREIGN_CLAIM = foreign_claim_release.FOREIGN_CLAIM
PROBE_CLAIM = 0xF0C6
SCRATCH_CLAIM = 0xF0C7

# The doctored case's named evidence — carried by both the doctored
# failure and the inconclusive-offers-no-evidence line so a predating
# release can never launder the unchecked self-check.
TAMPER_EVIDENCE = "the unsynchronized reclaim took the field"


def monitor_addr(url):
    """The peer's dialable monitor address as `host:port`."""
    return foreign_claim_release.monitor_addr(url)


def monitor_kind(declared, owner_addr):
    """Classify a claim-declared monitor — the sibling leg's
    `wildcard`/`owner`/`foreign` vocabulary."""
    return foreign_claim_release.monitor_kind(declared, owner_addr)


def pause_seat(process, what):
    """The incumbent's pause — `SIGSTOP` on the spawned controller
    process, the `docker pause` shape a deployed container takes: its
    sockets stay open, held by no answering process, while its claim
    stands holderless on the thawed field — the transport-frozen
    incumbent the finding reproduces. A lever that cannot land
    classifies inconclusive."""
    if not hasattr(signal, "SIGSTOP"):
        raise Inconclusive(
            "the consumer harness admits no stop/pause lever — no "
            "SIGSTOP"
        )
    try:
        process.send_signal(signal.SIGSTOP)
    except Exception as error:
        raise Inconclusive(
            f"the {what} pause lever never landed",
            f"{error}",
        )


def resume_seat(process):
    """The pause's restore — `SIGCONT` so the incumbent's next driven
    scan re-attaches on its live line."""
    try:
        process.send_signal(signal.SIGCONT)
    except Exception:
        pass


def frozen_scan(url, results, name):
    """One bounded `POST /scan` on a seat whose field is frozen — the
    driver's one-request-timeout-per-window cost the bounded-contact
    contract owes. A scan never answering inside the bound is the
    release predating it — inconclusive."""
    thread = threading.Thread(
        target=bounded_liveness.drive_scan,
        args=(url, results, name),
        daemon=True,
    )
    thread.start()
    thread.join(SCAN_BOUND_S)
    if thread.is_alive():
        raise Inconclusive(
            "the pinned release predates the bounded-contact "
            "backoff contract — the incumbent's scan against the "
            f"frozen field never answered inside the declared "
            f"{SCAN_BOUND_S}s bound",
            "the once-per-point stall the fix closed holds the "
            "scan past the bound",
        )
    return results.get(name)


def field_ping(client):
    """One bounded `ping` on a leg-held plant attachment — the thawed
    field's liveness answer. Returns the answered tick."""
    results = {}

    def probe():
        try:
            results["answer"] = client.request({"op": "ping"})
        except Exception as error:
            results["answer"] = error

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    thread.join(PING_BOUND_S)
    answer = results.get("answer")
    if thread.is_alive() or not isinstance(answer, dict) or (
        answer.get("result") != "alive"
    ):
        raise Inconclusive(
            "the thawed field never answered a ping inside the "
            f"declared {PING_BOUND_S}s bound — the pause/restore "
            "levers cannot stage the window",
            f"ping answered {answer!r}",
        )
    return answer


def gate_contract_surface(rig, verdict_io, probe_io, a_token, a_url,
                          failures, evidence):
    """The contract surface the episode asserts on: the standing
    claim fencing third-party mutations while naming the launch
    owner and its declared dialable monitor, the `reclaim_writer`
    and `probe_writer` verbs answered (the gate's probe reclaim
    refusing the live claim it shares no owner with), the served
    checkpoint's ownership stamps, and the durable journal files the
    manifest declares. Each absence is the release predating the
    contract, never a violation of it."""
    if rig.duty_files.get("journal_file") is None or (
        rig.standby_files.get("journal_file") is None
    ):
        raise Inconclusive(
            "the manifest's pair declares no journal files — the "
            "durable half of the gate audit is absent"
        )
    probe0 = verdict_io.request({"op": "step", "dt": 0})
    evidence["probe0"] = probe0
    if not claim_reclaim.mutation_fenced(probe0):
        failures.append(
            "the field held no writer claim after convergence — a "
            f"third-party probe answered {probe0}, so the leg has "
            "no standing claim to fence through"
        )
        raise Abort
    if claim_reclaim.verdict_owner(probe0) is None:
        raise Inconclusive(
            "the fencing verdict names no standing owner — the "
            "pinned release predates the verdict attribution the "
            "contract's claimants read",
            f"the standing fencing verdict was {probe0}",
        )
    if claim_reclaim.verdict_owner(probe0) != a_token:
        failures.append(
            "the standing claim names a foreign token, not the "
            f"launch owner — the pair is not in its launch claim "
            f"state: {probe0}"
        )
        raise Abort
    declared0 = stranded_rejoin.verdict_monitor(probe0)
    if declared0 is None:
        raise Inconclusive(
            "the standing claim declares no monitor — the pinned "
            "release predates the claim-declared monitor the "
            "ex-owner's rejoin resolves",
            f"the standing fencing verdict was {probe0}",
        )
    kind0 = monitor_kind(declared0, monitor_addr(a_url))
    if kind0 == "wildcard":
        raise Inconclusive(
            "the standing claim declares its wildcard bind verbatim "
            "— the pinned release predates the claim-monitor "
            "normalization",
            f"the declared monitor was {declared0}",
        )
    if kind0 != "owner":
        failures.append(
            f"the standing claim declares monitor {declared0} — not "
            f"the field owner's dialable monitor {monitor_addr(a_url)}"
        )
        raise Abort
    doc = pair.get(f"{a_url}/checkpoint", "GET /checkpoint", failures)
    evidence["doc0"] = doc
    if "source_owns_field" not in doc or "line_owner" not in doc:
        raise Inconclusive(
            "the served checkpoint carries no field-ownership "
            "stamps — the pinned release predates the "
            "field-arbitrated monitor contract",
            f"the checkpoint serves {sorted(doc)}",
        )
    # The bound reclaim verb: a foreign ask must refuse the launch
    # owner's live claim — the grant's live-holder half exercised
    # without mutating. A build predating the verb answers
    # invalid_request; a grant would be the contract itself broken.
    gate = probe_io.request({"op": "reclaim_writer", "owner": PROBE_CLAIM})
    evidence["reclaim_gate"] = gate
    if claim_reclaim.unsupported_verb(gate):
        raise Inconclusive(
            "reclaim_writer answered invalid_request — the pinned "
            "release predates the bound re-grant verb the gate "
            "arms",
            f"reclaim_writer answered {gate}",
        )
    if gate.get("result") in ("done", "claimed_shared"):
        failures.append(
            "a foreign reclaim grant took the live claim — the "
            "never-preempts-a-live-holder rule the gate's evidence "
            f"depends on is broken: {gate}"
        )
        raise Abort
    if not claim_reclaim.mutation_fenced(gate):
        raise Inconclusive(
            "reclaim_writer answered neither a grant nor a fencing "
            "refusal — the pinned release predates the grant's "
            "contract shape",
            f"reclaim_writer answered {gate}",
        )
    probe_w = probe_io.request({"op": "probe_writer"})
    evidence["probe_writer_gate"] = probe_w
    if claim_reclaim.unsupported_verb(probe_w):
        raise Inconclusive(
            "probe_writer answered invalid_request — the pinned "
            "release predates the claim-state probe the window's "
            "evidence reads",
            f"probe_writer answered {probe_w}",
        )
    if not claim_reclaim.mutation_fenced(probe_w):
        failures.append(
            "probe_writer does not report the standing claim held "
            f"— the claim-state observation answered {probe_w}"
        )
        raise Abort


def orphaned_watch(url, failures, evidence, label):
    """The tracked peer's ownerless-line verdict: driven scans pulling
    the demoted ex-owner's checkpoints until the report reads
    `orphaned` — the promotable posture the conditional orphan grant
    rides. Returns the observed sync-kind stream."""
    observed = []
    orphaned = None
    for _ in range(ORPHAN_SCANS):
        report = pair.get(f"{url}/role", "GET /role", failures)
        observed.append(stranded_rejoin.sync_kind(report))
        if stranded_rejoin.sync_kind(report) == "orphaned":
            orphaned = report
            break
        pair.scan(url, failures)
    evidence[label + "_orphan"] = observed
    if orphaned is None:
        if any(kind == "missing" for kind in observed):
            raise Inconclusive(
                "the tracking peer's role report carries no sync "
                "vocabulary — the pinned release predates the "
                "contract's orphaned verdict",
                f"the standby's sync readings were {observed}",
            )
        raise Inconclusive(
            "the tracking peer never reported the orphaned line — "
            "the pinned release predates the ownerless-line verdict "
            "the conditional claim rides",
            f"the standby's sync readings were {observed}",
        )
    return orphaned


def seat_incumbent(rig, b_url, verdict_io, failures, evidence, label):
    """The incumbent's seating: `POST /promote` on the orphaned
    standby lands its conditional claim over the tool's hold — the
    promoted peer's controller claim standing on its live line under
    its own token, its dialable monitor declared. Returns the
    incumbent's learned owner token."""
    rig.promote(
        b_url,
        failures,
        "the orphaned standby under the preempted field",
    )
    seated = None
    roles = []
    for _ in range(WATCH_SCANS):
        pair.scan(b_url, failures)
        report = pair.get(f"{b_url}/role", "GET /role", failures)
        roles.append(report.get("role"))
        if report.get("role") == "active":
            seated = report
            break
    evidence[label + "_seated"] = roles
    if seated is None:
        failures.append(
            "the promoted standby never settled active — its "
            "conditional claim over the tool's hold was refused: "
            f"{roles}"
        )
        raise Abort
    verdict = verdict_io.request({"op": "step", "dt": 0})
    evidence[label + "_verdict"] = verdict
    if not claim_reclaim.mutation_fenced(verdict):
        failures.append(
            "the seated incumbent's claim does not fence "
            f"third-party mutations: {verdict}"
        )
        raise Abort
    owner = claim_reclaim.verdict_owner(verdict)
    if owner is None:
        raise Inconclusive(
            "the incumbent's fencing verdict names no standing "
            "owner — the pinned release predates the verdict "
            "attribution the window's evidence reads",
            f"the fencing verdict was {verdict}",
        )
    declared = stranded_rejoin.verdict_monitor(verdict)
    if declared is None:
        raise Inconclusive(
            "the incumbent's claim declares no monitor — the pinned "
            "release predates the claim-declared monitor the "
            "ex-owner's rejoin resolves",
            f"the fencing verdict was {verdict}",
        )
    kind = monitor_kind(declared, monitor_addr(b_url))
    if kind == "wildcard":
        raise Inconclusive(
            "the incumbent's claim declares its wildcard bind "
            "verbatim — the pinned release predates the "
            "claim-monitor normalization",
            f"the declared monitor was {declared}",
        )
    if kind != "owner":
        failures.append(
            f"the incumbent's claim declares monitor {declared} — "
            f"not the promoted peer's dialable monitor "
            f"{monitor_addr(b_url)}"
        )
        raise Abort
    return owner


def race_window(
    rig,
    ctl,
    a_url,
    a_token,
    b_token,
    verdict_io,
    probe_io,
    plant_io,
    cmd,
    held_value,
    tamper,
    floor,
    failures,
    evidence,
):
    """The frozen-field race: the incumbent paused behind its
    holderless claim, driven scans on the fenced ex-owner — the
    bound reclaim armed, the convergence proof never standing —
    asserting the claim keeps naming the incumbent, the ex-owner
    never leaves `standby`, and the field records no write. The
    `opened-field` tamper dissolves the incumbent's claim first, so
    the unsynchronized reclaimer's legal unclaimed-field re-grant
    takes the field and every gate assertion must fire. A stale
    takeover on the untampered run is the pre-contract pin's shape —
    inconclusive, never a product failure. Returns the digest's
    watch rows."""
    if tamper == "opened-field":
        # The doctored window: a scratch attachment's unconditional
        # claim preempts the incumbent's holderless claim and hands
        # it back unclaimed — the opened field the unsynchronized
        # reclaimer may legally re-seat, the defect shape the leg's
        # gate assertions must still name.
        claim = probe_io.request(
            {"op": "claim_writer", "owner": SCRATCH_CLAIM}
        )
        evidence["scratch_claim"] = claim
        release = probe_io.request({"op": "release_writer"})
        evidence["scratch_release"] = release
        standing = probe_io.request({"op": "probe_writer"})
        evidence["scratch_probe"] = standing
        if failover.probe_kind(standing) != "unclaimed":
            raise Inconclusive(
                "the doctored open never landed — the harness's "
                "claim verbs cannot dissolve the holderless claim",
                f"claim/release/probe answered {claim} / {release} "
                f"/ {standing}",
            )
    watch = []
    ctl_seen = False
    for _ in range(RACE_SCANS):
        pair.scan(a_url, failures)
        report = pair.get(f"{a_url}/role", "GET /role", failures)
        kind = stranded_rejoin.sync_kind(report)
        watch.append(report.get("role") + "/" + kind)
        probe = verdict_io.request({"op": "step", "dt": 0})
        owner = claim_reclaim.verdict_owner(probe)
        held = failover.field_read(plant_io, cmd, failures)["value"]

        # The stale takeover: the ex-owner leaving standby, or the
        # verdict naming its recorded token — the unconverged
        # reclaimer preempting the standing claim the finding
        # reproduces. Untampered that is the predating pin's shape —
        # inconclusive; under the doctored open it is the defect the
        # gate assertions must name.
        stale = report.get("role") != "standby" or owner == a_token
        if stale:
            if tamper == "opened-field":
                failures.append(
                    "the incumbent's claim did not stand through "
                    "the frozen-field window — "
                    f"{TAMPER_EVIDENCE}: the fenced ex-owner "
                    f"reported {report.get('role')}/{kind} and the "
                    f"verdict names owner {owner}: {probe}"
                )
                raise Abort
            raise Inconclusive(
                "the pinned release predates the convergence-gated "
                "fencing-loss reclaim contract",
                "the unsynchronized ex-owner preempted the "
                "incumbent's standing claim — the stale-image "
                "takeover the gate refuses: the ex-owner reported "
                f"{report.get('role')}/{kind} and the verdict "
                f"names owner {owner}: {probe}",
            )
        if kind == "missing":
            raise Inconclusive(
                "the fenced ex-owner's role report carries no sync "
                "vocabulary — the pinned release predates the "
                "contract's standby verdicts",
                f"the role report was {report}",
            )
        if kind in ("tracking", "orphaned"):
            raise Inconclusive(
                "the fenced ex-owner reported convergence evidence "
                "the frozen incumbent could never serve — the "
                "pinned release predates the claimed-monitor "
                "resolution's verify gate",
                f"the role report was {report}",
            )
        if not claim_reclaim.mutation_fenced(probe):
            if failover.probe_kind(probe) == "unclaimed":
                failures.append(
                    "the incumbent's claim fell off the field "
                    "without the ex-owner taking it — the "
                    f"holderless fence contract broke: {probe}"
                )
            else:
                failures.append(
                    "the race window's fencing probe answered "
                    f"{probe} — expected the incumbent's standing "
                    "claim's refusal"
                )
            raise Abort
        if owner != b_token:
            failures.append(
                "the standing claim moved to a foreign token "
                f"{owner} during the reattach window — neither the "
                "incumbent's nor the ex-owner's: "
                f"{probe}"
            )
            raise Abort
        if held != held_value:
            failures.append(
                "the field moved under the incumbent's standing "
                f"claim — a foreign write landed: "
                f"{held_value} -> {held}"
            )
            raise Abort
        if not ctl_seen:
            ctl_step = foreign_claim_release.plant_ctl(
                ctl, rig.plant_addr, "step", "0"
            )
            ctl_list = foreign_claim_release.plant_ctl(
                ctl, rig.plant_addr, "list"
            )
            evidence["ctl_step"] = ctl_step[2].strip()
            evidence["ctl_list"] = ctl_list[1].strip()
            if ctl_step[0] == 0 or (
                foreign_claim_release.FENCED_DETAIL not in ctl_step[2]
            ):
                failures.append(
                    "a dcs-plant-ctl step under the incumbent's "
                    "standing claim was not refused with the named "
                    f"fencing failure — exit {ctl_step[0]}: "
                    f"{ctl_step[2].strip() or ctl_step[1].strip()}"
                )
                raise Abort
            if ctl_list[0] != 0 or '"points"' not in ctl_list[1]:
                failures.append(
                    "dcs-plant-ctl list did not answer the field "
                    "census under the standing claim — exit "
                    f"{ctl_list[0]}: {ctl_list[2].strip()}"
                )
                raise Abort
            ctl_seen = True
    evidence["race_watch"] = watch

    # The window's journal evidence: the ex-owner never walking a
    # role — no promoting, no active — while the claim stood, and
    # the incumbent never losing it.
    a_added = pair.get(f"{a_url}/journal", "GET /journal", failures)[
        floor:
    ]
    if stranded_rejoin.role_walk(a_added):
        if tamper == "opened-field":
            failures.append(
                "the incumbent's claim did not stand through the "
                f"frozen-field window — {TAMPER_EVIDENCE}: the "
                "fenced ex-owner journaled the role walk "
                f"{stranded_rejoin.role_walk(a_added)}"
            )
            raise Abort
        raise Inconclusive(
            "the pinned release predates the convergence-gated "
            "fencing-loss reclaim contract",
            "the unsynchronized ex-owner journaled a role walk "
            "toward the field — the stale-image takeover the gate "
            f"refuses: {stranded_rejoin.role_walk(a_added)}",
        )
    if stranded_rejoin.lost_entries(a_added):
        failures.append(
            "the fenced ex-owner journaled a second fencing loss "
            "while the incumbent's claim stood — a standing claim "
            "is observed, never re-lost: "
            f"{stranded_rejoin.lost_entries(a_added)}"
        )
        raise Abort
    return watch


def reclaim_convergence_pass(args, tamper):
    """The reclaim-convergence-gate run: launch the unkeyed
    declared-binds pair, converge, gate the contract surface, fence
    the born-active seat to `standby`/`unsynchronized`, seat the
    sibling as the incumbent, freeze the field and the incumbent's
    transport, run the armed-reclaim race across the thaw window
    asserting the unsynchronized reclaim never preempts, then prove
    the converged fenced ex-owner's bound reclaim of the incumbent's
    released claim — restoring the pair's launch roles. Returns
    `(digest_entries, evidence, failures)`; raises `Inconclusive`
    where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "reclaim-convergence-gate leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    rig = verdict_io = foreign_io = probe_io = None
    a_url = b_url = None
    plant_paused = b_paused = False
    try:
        rig = foreign_claim_release.launch_unkeyed(args, declared)
        a_url, b_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io
        # The claim attachments the episode stages through — the
        # rig's own client stays read-only: `verdict_io` runs the
        # third-party mutation probes whose fencing verdicts carry
        # the standing claim's owner and declared monitor,
        # `foreign_io` holds the monitor-less tool claim the fenced
        # demotion stages, and `probe_io` runs the read-only claim
        # observations, the gate's foreign reclaim ask, and the
        # doctored case's scratch claim.
        verdict_io = simulate.PlantClient(rig.plant_addr)
        foreign_io = simulate.PlantClient(rig.plant_addr)
        probe_io = simulate.PlantClient(rig.plant_addr)
        with open(args.model) as handle:
            model = json.load(handle)
        points = failover.signal_points(model)
        if points is None:
            raise Abort(
                "the emitted model declares no p101-cmd/level-primary "
                "signal points — the leg has no field point to watch"
            )
        cmd = points["cmd"]

        # Phase 1 — the declared wildcard binds deployed verbatim:
        # the unkeyed corner's own deployment shape.
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

        # Phase 2 — convergence and the contract surface.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "binds": binds,
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )
        a_token = failover.owner_token(rig.duty_preamble)
        if a_token is None:
            raise Inconclusive(
                "the launched active recorded no claim line — the "
                "pinned release claims only on promotion, predating "
                "the claim lifecycle the convergence gate rides on"
            )
        gate_contract_surface(
            rig, verdict_io, probe_io, a_token, a_url, failures, evidence
        )
        digest_entries.append(
            {
                "phase": "gate",
                "probe": "fenced",
                "owner": "named",
                "monitor": "owner",
                "reclaim": "fenced",
                "claim_probe": "answered",
            }
        )

        # Phase 3 — the born-active seat fenced to
        # `standby`/`unsynchronized`: the dedicated attachment's
        # monitor-less tool claim preempts the launch owner, whose
        # first fenced write demotes it in place — the reclaim armed
        # by the loss mark with no convergence evidence behind it.
        floor_a = len(
            pair.get(f"{a_url}/journal", "GET /journal", failures)
        )
        floor_b = len(
            pair.get(f"{b_url}/journal", "GET /journal", failures)
        )
        foreign_claim_release.foreign_claim(
            foreign_io, None, evidence, "fenced"
        )
        seized = verdict_io.request({"op": "step", "dt": 0})
        evidence["seized"] = seized
        if not claim_reclaim.mutation_fenced(seized):
            failures.append(
                "the foreign claim's preempt did not fence the "
                f"field — a third-party probe answered {seized}"
            )
            raise Abort
        if claim_reclaim.verdict_owner(seized) != FOREIGN_CLAIM:
            if claim_reclaim.verdict_owner(seized) is None:
                raise Inconclusive(
                    "the post-preemption fencing verdict names no "
                    "standing owner — the pinned release predates "
                    "the verdict attribution",
                    f"the fencing verdict was {seized}",
                )
            failures.append(
                "the fencing verdict attributes the preempted "
                "claim to "
                f"{claim_reclaim.verdict_owner(seized)}, not the "
                f"foreign token {FOREIGN_CLAIM:#x}: {seized}"
            )
            raise Abort
        if "monitor" in ((seized.get("error")) or {}):
            failures.append(
                "the monitor-less foreign claim names a monitor "
                f"in the fencing verdict: {seized}"
            )
            raise Abort
        roles = foreign_claim_release.fenced_demotion(
            rig, a_url, floor_a, failures, evidence, "fenced"
        )
        demoted = pair.get(f"{a_url}/role", "GET /role", failures)
        if demoted.get("role") != "standby" or (
            stranded_rejoin.sync_kind(demoted) != "unsynchronized"
        ):
            raise Inconclusive(
                "the fenced ex-owner did not settle "
                "standby/unsynchronized — the pinned release "
                "predates the sourceless-standby contract the "
                "armed reclaim's window needs",
                f"the demoted report was {demoted}",
            )
        digest_entries.append(
            {
                "phase": "fenced",
                "seized": failover.probe_kind(seized),
                "watch": roles,
                "seat": "standby/unsynchronized",
            }
        )

        # Phase 4 — the incumbent: the orphaned sibling's conditional
        # claim preempts the tool's hold — the controller claim
        # standing on its live line under its own token, its dialable
        # monitor declared. The fenced ex-owner is deliberately not
        # scanned past this point: its unconverged stance is the
        # window's subject.
        orphaned_watch(b_url, failures, evidence, "incumbent")
        b_token = seat_incumbent(
            rig, b_url, verdict_io, failures, evidence, "incumbent"
        )
        if b_token in (a_token, FOREIGN_CLAIM):
            failures.append(
                "the incumbent's claim token was not learned from "
                f"the fencing verdict: {b_token}"
            )
            raise Abort
        # The field snapshot the window guards — the incumbent's
        # ownership epoch's face value; only a write could move it.
        held_value = failover.field_read(plant_io, cmd, failures)[
            "value"
        ]
        incumbent_snap = pair.get(
            f"{b_url}/snapshot", "GET /snapshot", failures
        )
        incumbent_tick = incumbent_snap["tick"]
        digest_entries.append(
            {
                "phase": "incumbent",
                "orphan": "reported",
                "promote": "granted",
                "seated": "active",
                "owner": "sibling",
                "monitor": "owner",
            }
        )

        # Phase 5 — the freeze: the plant's process paused, the
        # incumbent's bounded scans dropping its link — the claim
        # left standing holderless — then the incumbent paused
        # itself before the thaw, so its reattach races the armed
        # reclaim's probes. Both journal floors land before the
        # pause — a stopped peer serves no monitor reads.
        freeze_floor_a = len(
            pair.get(f"{a_url}/journal", "GET /journal", failures)
        )
        freeze_floor_b = len(
            pair.get(f"{b_url}/journal", "GET /journal", failures)
        )
        ownerless_backoff.pause_plant(rig)
        plant_paused = True
        try:
            results = {}
            dropped = False
            for attempt in range(FROZEN_SCANS):
                answer = frozen_scan(b_url, results, attempt)
                if not isinstance(answer, dict) or not isinstance(
                    answer.get("tick"), int
                ):
                    failures.append(
                        "the incumbent's POST /scan under the "
                        f"frozen field answered {answer!r} — "
                        "expected a served snapshot"
                    )
                    raise Abort
                snap = pair.get(
                    f"{b_url}/snapshot", "GET /snapshot", failures
                )
                health = driver_recovery.driver_health(snap)
                if health is None:
                    raise Inconclusive(
                        "the incumbent's io_health carries no "
                        "driver diagnostics — the pinned release "
                        "predates the link-state reporting the "
                        "freeze evidence reads",
                        f"the snapshot serves {sorted(snap)}",
                    )
                if health.get("link") == "disconnected":
                    dropped = True
                    break
            if not dropped:
                raise Inconclusive(
                    "the incumbent's link never dropped under the "
                    "frozen field — the pinned release predates "
                    "the bounded-contact contract the window "
                    "stages through",
                    f"the driver health reported {health}",
                )
            pause_seat(rig.standby, "incumbent")
            b_paused = True
        finally:
            ownerless_backoff.resume_plant(rig)
            plant_paused = False
        # The thaw's ordering margin: the resumed server reaps the
        # dropped connection — the claim holderless — before the
        # race's first probe; a ping confirms the field answers
        # again first.
        field_ping(verdict_io)
        time.sleep(REAP_MARGIN_S)
        digest_entries.append(
            {"phase": "freeze", "link": "dropped", "claim": "standing"}
        )

        # Phase 6 — the race: the armed reclaim's probes across the
        # thaw window while the incumbent's claim stands holderless
        # — the unsynchronized ex-owner must never preempt it.
        watch = race_window(
            rig,
            args.plant_ctl,
            a_url,
            a_token,
            b_token,
            verdict_io,
            probe_io,
            plant_io,
            cmd,
            held_value,
            tamper,
            freeze_floor_a,
            failures,
            evidence,
        )
        digest_entries.append(
            {
                "phase": "race",
                "watch": watch,
                "seat": "standby/unsynchronized",
                "claim": "incumbent",
            }
        )

        # Phase 7 — the incumbent's reattach on its live line: the
        # resumed process's next driven scans re-bind its own claim
        # — the ownership epoch surviving the freeze — its writes
        # landing and its journal showing no loss.
        resume_seat(rig.standby)
        b_paused = False
        time.sleep(REATTACH_WAIT)
        reattach = []
        for _ in range(RESOLVE_SCANS):
            pair.scan(b_url, failures)
            report = pair.get(f"{b_url}/role", "GET /role", failures)
            reattach.append(report.get("role"))
            if report.get("role") != "active":
                failures.append(
                    "the incumbent left active across its "
                    "reattach — its own claim's re-bind should "
                    f"keep the ownership epoch: {report}"
                )
                raise Abort
            verdict = verdict_io.request({"op": "step", "dt": 0})
            if claim_reclaim.verdict_owner(verdict) == b_token:
                break
        else:
            failures.append(
                "the reattached incumbent's claim never re-named "
                "its token in the fencing verdicts: "
                f"{reattach}"
            )
            raise Abort
        evidence["reattach"] = reattach
        if not claim_reclaim.mutation_fenced(verdict):
            failures.append(
                "the incumbent's reattach left the field unfenced "
                f"to third-party mutations: {verdict}"
            )
            raise Abort
        # The epoch's continuity: the incumbent's tick ran past its
        # pre-freeze mark and the field carries its own image —
        # never rolled back to the ex-owner's staler one.
        owner_snap = pair.get(
            f"{b_url}/snapshot", "GET /snapshot", failures
        )
        if owner_snap["tick"] <= incumbent_tick:
            failures.append(
                "the incumbent's run tick rolled back across the "
                "freeze — "
                f"{incumbent_tick} -> {owner_snap['tick']}"
            )
            raise Abort
        held = failover.field_read(plant_io, cmd, failures)["value"]
        if held != simulate.snapshot_point(owner_snap, cmd):
            failures.append(
                "the incumbent's reattach did not keep its "
                "ownership epoch — the field carries "
                f"{held} while its image reports "
                f"{simulate.snapshot_point(owner_snap, cmd)}"
            )
            raise Abort
        b_added = pair.get(
            f"{b_url}/journal", "GET /journal", failures
        )[freeze_floor_b:]
        if stranded_rejoin.lost_entries(b_added) or (
            stranded_rejoin.role_walk(b_added)
        ):
            failures.append(
                "the incumbent journaled a claim loss or role "
                "change across the frozen-field window — its "
                "reattach must keep the ownership epoch: "
                f"{b_added}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "reattach",
                "owner": "incumbent",
                "writes": "landed",
                "journal": "undisturbed",
            }
        )

        # Phase 8 — the convergence proof: driven scans on the
        # fenced ex-owner resolve the incumbent's claim-declared
        # monitor past the failed-verify retry window — the
        # journaled `tracking_source_adopted` — converging
        # `tracking`. The reclaim never issued while no proof
        # stood; the proof now standing is the positive control's
        # gate.
        rejoin = []
        tracked = None
        for _ in range(RESOLVE_SCANS):
            pair.scan(a_url, failures)
            report = pair.get(f"{a_url}/role", "GET /role", failures)
            kind = stranded_rejoin.sync_kind(report)
            rejoin.append(report.get("role") + "/" + kind)
            if claim_reclaim.tracking(report):
                tracked = report
                break
            if report.get("role") != "standby":
                failures.append(
                    "the fenced ex-owner left standby while "
                    "rejoining the incumbent — before the verified "
                    "source converges it may only report standby: "
                    f"{report}"
                )
                raise Abort
            time.sleep(REJOIN_SPACING_S)
        evidence["rejoin"] = rejoin
        if tracked is None:
            raise Inconclusive(
                "the fenced ex-owner never converged on the "
                "claim-declared successor — the pinned release "
                "predates the field-arbitrated tracking "
                "resolution the convergence proof rides",
                f"its reports stayed {rejoin}",
            )
        a_journal = pair.get(
            f"{a_url}/journal", "GET /journal", failures
        )
        adopted = stranded_rejoin.adopted_entries(a_journal[floor_a:])
        if len(adopted) != 1 or not str(
            adopted[0][1].get("source")
        ).endswith(":" + stranded_rejoin.monitor_port(b_url)):
            failures.append(
                "the ex-owner's convergence did not journal the "
                "claim-declared adoption naming the incumbent's "
                f"monitor — {adopted}"
            )
            raise Abort
        digest_entries.append(
            {"phase": "rejoin", "seat": "tracking", "adopted": "named"}
        )

        # Phase 9 — the positive control: the incumbent's deliberate
        # demotion leaves its claim standing yielded and holderless;
        # the now-converged fenced ex-owner's bound reclaim preempts
        # it — the unattended `standby → promoting → active` walk
        # under the `reclaim` origin — the released claim re-seated
        # to the standing owner.
        reclaim_floor_a = len(a_journal)
        rig.demote(
            b_url,
            failures,
            "the incumbent under the window it survived",
        )
        for _ in range(WATCH_SCANS):
            pair.scan(b_url, failures)
            report = pair.get(f"{b_url}/role", "GET /role", failures)
            if report.get("role") == "standby":
                break
        else:
            failures.append(
                "the demoted incumbent never settled standby — the "
                "released claim's yielded hand-back never landed"
            )
            raise Abort
        released = probe_io.request({"op": "probe_writer"})
        evidence["released"] = released
        if claim_reclaim.unsupported_verb(released):
            raise Inconclusive(
                "probe_writer answered invalid_request — the "
                "pinned release predates the claim-state probe",
                f"probe_writer answered {released}",
            )
        if failover.probe_kind(released) == "unclaimed" or not (
            claim_reclaim.mutation_fenced(released)
        ):
            raise Inconclusive(
                "the demoted incumbent's claim did not stand "
                "holderless — the pinned release predates the "
                "yielded hand-back the bound reclaim preempts",
                f"probe_writer answered {released}",
            )
        if claim_reclaim.verdict_owner(released) != b_token:
            failures.append(
                "the released claim does not stand under the "
                f"incumbent's token — the fencing verdict names "
                f"{claim_reclaim.verdict_owner(released)}: "
                f"{released}"
            )
            raise Abort
        watch = []
        promoted = None
        for _ in range(RESOLVE_SCANS):
            pair.scan(a_url, failures)
            report = pair.get(f"{a_url}/role", "GET /role", failures)
            watch.append(
                report.get("role")
                + "/"
                + stranded_rejoin.sync_kind(report)
            )
            if report.get("role") == "active":
                promoted = report
                break
            if report.get("role") not in ("standby", "promoting"):
                failures.append(
                    "the converged ex-owner reported "
                    f"{report.get('role')} reclaiming the released "
                    "claim — it may only walk back toward "
                    f"active: {report}"
                )
                raise Abort
        evidence["reclaim"] = watch
        if promoted is None:
            failures.append(
                "the converged ex-owner's bound reclaim never "
                "took the incumbent's released claim — the "
                f"positive control wedged: {watch}"
            )
            raise Abort
        verdict = verdict_io.request({"op": "step", "dt": 0})
        evidence["reclaim_verdict"] = verdict
        if not claim_reclaim.mutation_fenced(verdict) or (
            claim_reclaim.verdict_owner(verdict) != a_token
        ):
            failures.append(
                "the reclaimed claim does not stand under the "
                "ex-owner's recorded token — the fencing verdict "
                f"answered {verdict}"
            )
            raise Abort
        declared_a = stranded_rejoin.verdict_monitor(verdict)
        if declared_a is None:
            raise Inconclusive(
                "the reclaimed claim declares no monitor — the "
                "pinned release predates the claim-declared "
                "monitor the contract carries",
                f"the fencing verdict was {verdict}",
            )
        kind_a = monitor_kind(declared_a, monitor_addr(a_url))
        if kind_a == "wildcard":
            raise Inconclusive(
                "the reclaimed claim declares its wildcard bind "
                "verbatim — the pinned release predates the "
                "claim-monitor normalization",
                f"the declared monitor was {declared_a}",
            )
        if kind_a != "owner":
            failures.append(
                f"the reclaimed claim declares monitor "
                f"{declared_a} — not the ex-owner's dialable "
                f"monitor {monitor_addr(a_url)}"
            )
            raise Abort
        a_journal = pair.get(
            f"{a_url}/journal", "GET /journal", failures
        )
        walk_a = stranded_rejoin.role_walk(a_journal[reclaim_floor_a:])
        if ("standby", "promoting", "reclaim") not in walk_a or (
            "promoting",
            "active",
            "reclaim",
        ) not in walk_a:
            failures.append(
                "the converged reclaim left no journaled walk "
                "back to active under the `reclaim` origin — an "
                f"operator or restart path ran instead: {walk_a}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "reclaim",
                "walk": "reclaim",
                "owner": "exowner",
                "monitor": "owner",
            }
        )

        # Phase 10 — the restore: the demoted incumbent rejoins the
        # reclaimed owner through its declared `--standby` wiring and
        # the driven tracking-first ticks prove the pair's images
        # identical — the launch roles standing: the manifest's duty
        # controller `active`, its declared standby `tracking`.
        ticks = []
        for _ in range(pair.HANDOVER_TICKS):
            _tracked, owner = rig.tick(
                b_url,
                a_url,
                failures,
                diverged="the restored pair's images diverged at "
                "tick {tick} — the convergence-gated reclaim was "
                "not bumpless",
            )
            ticks.append(owner["tick"])
        if not driver_recovery.roles_hold(
            rig, failures, "after the frozen-field episode"
        ):
            raise Abort
        held = failover.field_read(plant_io, cmd, failures)["value"]
        if held != simulate.snapshot_point(owner, cmd):
            failures.append(
                "the re-seated claim fences the owner's own "
                f"writes — the field carries {held} while its "
                f"image reports {simulate.snapshot_point(owner, cmd)}"
            )
            raise Abort
        # The durable mirror: the manifest-declared journal files
        # carry the whole episode — the ex-owner's attributed loss
        # beside its fenced demotion, its claimed-monitor adoption,
        # and the reclaim walk — and the incumbent's file shows no
        # loss at all.
        a_durable = stranded_rejoin.journal_entries(
            rig.duty_files["journal_file"]
        )
        a_losses = stranded_rejoin.lost_entries(a_durable)
        if len(a_losses) != 1 or (
            a_losses[0][1].get("claimant") != FOREIGN_CLAIM
        ):
            failures.append(
                "the ex-owner's durable journal carries the losses "
                f"{a_losses} — expected exactly one "
                "field_claim_lost attributed to the foreign token"
            )
            raise Abort
        a_kinds = stranded_rejoin.durable_kinds(
            rig.duty_files["journal_file"]
        )
        for owed in ("field_claim_lost", "tracking_source_adopted"):
            if owed not in a_kinds:
                failures.append(
                    "the ex-owner's durable journal file carries "
                    f"no {owed} record — the journaled evidence "
                    "the convergence gate's audit owes"
                )
                raise Abort
        b_kinds = stranded_rejoin.durable_kinds(
            rig.standby_files["journal_file"]
        )
        if "field_claim_lost" in b_kinds:
            failures.append(
                "the incumbent's durable journal carries a "
                "field_claim_lost — its claim stood through the "
                "whole window"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "restore",
                "ticks": ticks,
                "duty_role": "active",
                "standby_role": "tracking",
                "a_durable": sorted(a_kinds),
                "b_durable": sorted(b_kinds),
            }
        )
        evidence["final_tick"] = ticks[-1]
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        # Restore every pause/stop lever before the rig's teardown:
        # a paused incumbent's terminate waits out the stop timeout,
        # and a paused plant never drains.
        if b_paused and rig is not None and rig.standby is not None:
            resume_seat(rig.standby)
        if plant_paused and rig is not None and rig.plant is not None:
            ownerless_backoff.resume_plant(rig)
        # Detach the claim attachments cleanly: release whatever
        # hold each still carries — a hold left standing keeps the
        # field claimed for a dead token — then close.
        for client in (verdict_io, foreign_io, probe_io):
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
    parser.add_argument("--plant-ctl", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--tamper",
        choices=["opened-field"],
        help="doctor the frozen-field window open — the incumbent's "
        "holderless claim preempted and released under a scratch "
        "token — so the unsynchronized reclaimer takes the field "
        "and the leg's gate assertions must report the named "
        "diagnostic",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = (
            reclaim_convergence_pass(args, args.tamper)
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"reclaim-convergence-gate: {TAMPER_EVIDENCE} — an "
                "inconclusive run offers the doctored case no "
                "evidence"
            )
            return 1
        # The digest line carries only the stable reason — the
        # run's own verdicts and endpoints report on stderr, where
        # two identical passes need not share them.
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"reclaim-convergence-gate: inconclusive — {detail}")
        print(
            f"reclaim-convergence-gate-digest inconclusive — {reason}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"reclaim-convergence-gate: {line}")
        return 1
    for failure in failures:
        eprint(f"reclaim-convergence-gate: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"reclaim-convergence-gate: the {args.tamper} case "
                "passed silently — the leg never noticed the "
                "doctored window"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"reclaim-convergence-gate-digest {digest} — the fenced "
        "ex-owner's unsynchronized reclaim never preempted the "
        "frozen incumbent's standing claim, the incumbent's reattach "
        "kept its ownership epoch, the converged ex-owner's bound "
        "reclaim took the released claim under the `reclaim` origin, "
        f"and launch roles stand at tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
