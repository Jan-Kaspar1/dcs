#!/usr/bin/env python3
"""The claim-skew-bound leg for the reference plant — the
consumer-side proof that a claim whose *basis* is skewed past the
recorded bound cannot take write-ownership from a live incumbent
(WW-ENG-003, WW-LCM-001 — the contract #1336's fix records, mirrored
at the customer boundary alongside the qa rig's
`claim-skew-bound` scenario).

The recorded bound the leg stages against is the platform's recorded
window over run ticks: the thirty-two-tick `MAX_ANNOUNCED_AHEAD`
skew window `docs/architecture.md` carries for the line-membership
verifies (decision 91's amendment, #1269), retired outright by #1336
— no window stands against a *comparator's* own position any more,
because a detached prober's tick advances at its own scan cadence
while the line's stream advances at the owner's, so the gap measures
pace asymmetry and never line membership. What the tick-domain
comparability record keeps is the rule this leg exercises at the
claim boundary: **two stamps order only after a translation**, so a
claim must never be resolved in the claimant's favour on the strength
of a position the claimant alone asserts. A peer whose claim basis
sits past the recorded bound — the state a controller whose clock ran
ahead, or whose restored state came off such a host, comes up with —
asserts its write-ownership claim against a live incumbent, and the
incumbent keeps the field: the attempt is refused or bounded by name,
never a silent preemption that boots the real owner.

A driven deployment cannot skew a wall clock, so the leg stages the
skew on the axis the recorded bound and the comparability record are
stated in — the run's own tick domain — through the only lever a
customer deployment really holds: the peer's declared `--state-file`,
the persisted basis a controller resumes onto. The skewed claimant is
the pair's own launch shape — `dcs-controller --driven --remote`, no
`--peer`, no `--standby`, the duplicate seat an unkeyed manifest
admits beside the settled pair — resuming a checkpoint whose run tick
sits 4096 ticks past the live line's, orders of magnitude beyond the
recorded thirty-two-tick window, carrying the line's own generation
and fingerprint so it is a *continuation* of the line and not a
foreign run: exactly the claim a skewed-clock peer makes when it comes
up believing it is the line's own continuation. The honest basis it is
cut from is the declared standby's own persisted checkpoint, so the
doctored document stays a real checkpoint of this model and this line.

The run:

- converges the manifest-declared pair and gates the contract surface
  the leg's attribution reads — the launched field owner's claim line,
  the plant's claim-status observation naming that claim's owner and
  its declared monitor, the declared pair's `state_file` persistence
  the skew is cut from, an integer run tick on the served reports.
  Each absence is the release predating the substrate, reported
  `claim-skew-bound-digest inconclusive`, never a failure;
- proves an **inside-bound** claim still resolves normally: a
  dedicated plant-socket attachment reads the standing claim (the
  read-only claim-status probe answering the named fencing verdict),
  asserts the claim on the line's own basis — the incumbent's own
  owner token, no skew at all — which joins the standing claim under
  the recorded shared hold, mutates the field under that hold, and
  releases it, leaving the claim standing on the incumbent. The
  arbitration is therefore live and resolving claims on the line's own
  position, so the skewed attempt's refusal is attributable to the
  skew and not to a claim path that refuses everything;
- stages the skew: the declared standby's persisted checkpoint copied
  and doctored onto a run tick 4096 past the incumbent's live tick,
  then a third controller launched on the pair's born-active shape at
  the deployed plant with that state file. Its resumed basis is read
  back from its own startup record and must sit past the recorded
  thirty-two-tick window — a staging that accrues no overrun is the
  release predating the resumed-basis claim this leg skews, never an
  assertion;
- asserts the incumbent keeps write-ownership across the attempt: the
  plant's claim-status observation keeps naming the incumbent's own
  owner token and its declared monitor (a token that moved is the
  winner-take-all preemption this contract closed — the pinned
  release, reported `inconclusive`), the pair's launch roles hold and
  its tick advances, neither peer's journal gains a claim, role,
  divergence, restart, or command record, and the skewed claimant
  never served `active` or `promoting`;
- asserts the attempt was refused or bounded **by name**: the
  claimant's own record — its startup output and the durable journal
  file it declared — carries the write-ownership refusal, the
  observed claimant naming the live incumbent's own token, and the
  documented `--standby ADDRESS` remedy for the pairless seat. A
  refusal naming only the standby *role* it would have landed in, with
  no flag an operator can type, is the pinned release predating the
  named disposition — reported `inconclusive`, never an assertion;
- proves the clean pair settles, tracks, and promotes afterwards: the
  declared pair rests on its launch roles across the settle ticks, runs
  the documented switch — `POST /demote` on the field owner,
  `POST /promote` on the converged standby — with the field's
  write-ownership claim moving to the promoted peer and the demoted
  peer settling back on `tracking`, then runs it once more to re-seat
  the launch roles and the launch owner's claim for the legs behind
  this one.

The contract postdates the pinned release line. Where the launched
tooling predates it — no claim line on the launched owner, a claim
verdict naming no owner or no declared monitor, a resumed basis that
never accrues the staged overrun, a claim-status verb answered
`invalid_request`, a refusal whose disposition names no
`--standby ADDRESS` remedy — or where the claim resolves in the
skewed claimant's favour at all, the run reports
`claim-skew-bound-digest inconclusive` rather than asserting until the
manifest repins a release carrying the contract.

Usage:

    claim_skew_bound.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `claim-skew-bound-digest <sha256>` line prints — the
check runs two passes and compares them
(`claim-skew-bound-nondeterministic`). A contract violation reports
`claim-skew-bound: …` lines on stderr and exits 1 — the check's
`claim-skew-bound-failed`. `--tamper expect-preempt` doctors the
leg's own expectation to the defect shape — asserting the skewed
claimant took write-ownership from the live incumbent — so the leg
proves its incumbent-held assertion fires on the honest refusal
rather than passing an unexercised contract.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import born_active_failure
import claim_reclaim
import driver_recovery
import failover
import pair
import remote_foreign_model
import simulate
import stranded_rejoin


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored case.
# The doctored case: a leg asserting the skewed claimant took the
# field from the live incumbent — the preemption the bound closes —
# must surface the named diagnostic on the honest refusal rather than
# passing an unexercised contract.
LEG = {
    # The next free slot after the claim-class legs origin/main added
    # (remote-foreign-model 700, ownerless-backoff 710,
    # deferred-startup-refusal 720) — the stage runs the legs in this
    # order and no two may share one.
    "order": 730,
    "title": "the claim-skew-bound leg",
    "passes": "claim-skew-bound-leg",
    "failed": "claim-skew-bound-failed",
    "tampers": [
        {
            "name": "expect-preempt",
            "passed": "an expect-preempt case passed the claim-skew-bound leg",
            "missed": "the expect-preempt case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the skewed claimant to take the field"
            ],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the claim-skew bound this leg
    exercises — or the staged skew never landed, and the leg has no
    claim to referee: carried as `(reason, detail)`, `reason` the
    stable phrase the `inconclusive` digest line prints (two passes
    must share it) and `detail` the run's own verdicts, reported on
    stderr only, where minted owner tokens and ephemeral endpoints
    belong. A claim that resolved *for* the skewed claimant, the
    recorded window's whole subject, is the pre-contract shape the
    bound closed and is reported here — never as a product failure
    of a release that simply predates the contract."""


# The recorded bound the staging overruns — the thirty-two-tick
# `MAX_ANNOUNCED_AHEAD` skew window `docs/architecture.md` records for
# the line-membership verifies (retired by #1336, which is what makes
# the claim boundary the only place a skewed basis can still decide
# an arbitration). The staged overrun is orders of magnitude past it,
# so the staged claimant's basis cannot be mistaken for the honest
# accrued lead a tracking peer legitimately carries.
RECORDED_BOUND = 32
SKEW_TICKS = 4096

# The driven pair ticks the attempt is observed across (the pair keeps
# scanning while the skewed claimant asks, proving the incident left
# the line undisturbed), the settle ticks the pair rests on its launch
# roles after the attempt, and the wall-clock bound the refused
# claimant's nonzero exit lands inside.
WINDOW_TICKS = 4
SETTLE_TICKS = 3
EXIT_WAIT = 10.0

# The refusal vocabulary the skewed claimant's own record must carry —
# the write-ownership refusal the conditional startup grant produced,
# and the recorded disposition's remedy: the flag an operator types,
# not merely the standby role the seat would have landed in. A refusal
# naming the role without the flag is the release predating that
# disposition, reported inconclusive.
REFUSAL_MARKS = (
    "field write-ownership claim failed",
    "a live peer holds the field's write-ownership claim",
)
REMEDY_MARK = "--standby ADDRESS"
PAIRLESS_MARK = "no --peer was declared"

# The doctored expectation's stable evidence prefix — every failure
# the tampered pass records carries it, so the check's negative case
# finds it whether the honest run refused the claim or a predating
# release offered the case no evidence at all.
TAMPER_EVIDENCE = (
    "the doctored expectation wanted the skewed claimant to take the "
    "field"
)


def claim_owner(probe_io):
    """The plant's read-only claim-status answer — the observation a
    third attachment reads the standing claim through: the named
    fencing verdict carrying the standing claim's owner token and its
    declared monitor, `done` while this attachment holds the claim
    itself, or `unclaimed` while none stands. Never a mutation, so
    reading the field's ownership can never move it."""
    return probe_io.request({"op": "probe_writer"})


def resumed_tick(lines):
    """The run tick a resumed launch's startup record reports — the
    `resumed from state file … at tick N` line the `--state-file`
    resume writes — or None where no line carries one. The leg's own
    evidence that the staged skew actually landed on the launched
    claimant's clock."""
    for line in lines:
        resumed = re.search(r"at tick (\d+)", line)
        if "resumed from state file" in line and resumed:
            return int(resumed.group(1))
    return None


def skew_basis(source, scratch, skew):
    """The skewed claim basis: the declared peer's own persisted
    checkpoint copied and doctored onto a run tick `skew` past the one
    it carries — the state a controller whose clock ran ahead, or whose
    restored state came off such a host, comes up with. Every other
    field is the capturing run's own: the line's generation and
    fingerprint, its format version, and its declared stream position
    (which stays where the line left it — the honest accrued lead a
    peer legitimately carries), so the doctored document is still a
    real checkpoint of this model and this line, resumed as the line's
    own continuation rather than refused as a foreign document. The
    checked-in deployment is untouched; the copy lives in the leg's
    runner-owned scratch. Returns `(path, seeded_tick)`."""
    with open(source) as handle:
        document = json.load(handle)
    tick = document.get("tick")
    if not isinstance(tick, int):
        raise Abort(
            f"the declared peer's persisted checkpoint {source} carries "
            f"no integer run tick: {document.get('tick')!r} — the leg "
            "has no honest basis to skew"
        )
    skewed = dict(document)
    skewed["tick"] = tick + skew
    path = os.path.join(scratch, "skewed-basis.json")
    with open(path, "w") as handle:
        json.dump(skewed, handle)
    return path, skewed["tick"]


def refusal_line(lines):
    """The startup/output line naming the write-ownership refusal, or
    None where no line carries one."""
    for line in lines:
        if any(mark in line for mark in REFUSAL_MARKS):
            return line
    return None


def durable_observed(path):
    """The `field_claim_observed` claimants a launched run's declared
    durable journal file carries — the claim the run *saw* before its
    own claim resolved against it. A skewed claimant that observed
    the incumbent's own token holds the strongest named evidence that
    its claim was arbitrated, not ignored. Entries only: a launch
    that never opened the sink records nothing."""
    if not os.path.exists(path):
        return []
    return [
        record["claimant"]
        for entry in stranded_rejoin.journal_entries(path)
        if "field_claim_observed" in entry.get("event", {})
        for record in [entry["event"]["field_claim_observed"]]
    ]


def durable_kinds(path):
    """The event kinds the claimant's declared durable journal file
    carries — the run's own named record of the attempt."""
    if not os.path.exists(path):
        return set()
    return stranded_rejoin.durable_kinds(path)


def trailing_stderr(process):
    """The rest of an exited claimant's stderr — the lines after the
    `listening on` a run serving then refusing writes. Only read once
    the process is known dead."""
    try:
        return [line.strip() for line in process.stderr if line.strip()]
    except Exception:
        return []


def refusal_disposition(lines, code, failures):
    """The disposition a refused launch settled on, judged on that
    launch's own record: the write-ownership refusal named, the process
    ended nonzero inside the bound, and — on the pairless seat — the
    documented `--standby ADDRESS` remedy and the undeclared pair beside
    it. A refusal naming only the standby role it would have landed in,
    with no flag an operator can type, is the pinned release predating
    that disposition: `Inconclusive`, so the leg reports its named
    verdict rather than asserting on it. A record naming no refusal at
    all, or a launch that ended zero, is a failure."""
    output = " ".join(lines)
    named = refusal_line(lines)
    if named is None:
        if any(
            word in output
            for word in born_active_failure.PREDATING_WORDS
        ):
            raise Inconclusive(
                "the pinned release predates the claim-skew bound — "
                "the skewed claimant ended naming no write-ownership "
                "refusal",
                f"the launch reported {lines}",
            )
        failures.append(
            "the skewed claimant ended naming no write-ownership "
            f"refusal: {'; '.join(lines) or 'no diagnostic'}"
        )
        raise Abort
    if code == 0:
        failures.append(
            "the skewed claimant exited zero — a refused "
            "write-ownership claim is the launch's failure, never a "
            "silent settle"
        )
        raise Abort
    if REMEDY_MARK not in output:
        raise Inconclusive(
            "the pinned release predates the claim-skew bound's named "
            "disposition — the refusal names the standby role, not "
            "the --standby remedy an operator types",
            f"the launch reported {named}",
        )
    if PAIRLESS_MARK not in output:
        failures.append(
            "the pairless seat's refusal never names the undeclared "
            "pair — an operator reading the exit cannot tell which "
            f"remedy applies: {named}"
        )
        raise Abort
    return "refused-by-name"


def pair_undisturbed(rig, floors, failures, when):
    """The deployed pair across the attempt — one tracking-first pair
    tick, the launch-roles poll, and both peers' journals read above
    the floors the attempt opened: no claim, role, divergence, restart,
    command, or run-boundary record landed on either peer (the
    disturbance a leaked claim leaves), while the field owner stays
    `active` and the declared standby tracks it. Returns False when the
    incident moved the pair."""
    held = driver_recovery.roles_hold(rig, failures, when)
    for url, name, floor in floors:
        journal = pair.get(f"{url}/journal", "GET /journal", failures)
        leaked = [
            entry
            for entry in journal[floor:]
            if remote_foreign_model.DISTURBANCE_EVENTS
            & set(entry.get("event", {}))
        ]
        if leaked:
            failures.append(
                f"{name}'s journal gained disturbance records {when}: "
                f"{leaked}"
            )
            held = False
    return held


def claim_skew_bound_pass(args, tamper):
    """The claim-skew-bound run: converge the declared pair, gate the
    claim surface, prove an inside-bound claim resolves normally,
    stage a claimant whose restored basis sits past the recorded bound,
    assert the live incumbent keeps write-ownership and the attempt is
    refused or bounded by name, and restore the pair through the
    documented switch. Returns `(digest_entries, evidence, failures)`;
    raises `Inconclusive` where the pinned release predates the
    contract or the staged skew never landed."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "claim-skew-bound leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    scratch = tempfile.mkdtemp(prefix="dcs-claim-skew-")
    rig = probe_io = None
    claimant = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        # The dedicated attachment the claim-status observation and the
        # inside-bound control run through — read-only wherever it
        # watches, a declared holder only while it proves the
        # inside-bound claim resolves.
        probe_io = simulate.PlantClient(rig.plant_addr)

        # Phase 1 — convergence and the claim surface: the launched
        # field owner's own claim token (the attribution every verdict
        # below is read against), the claim-status observation naming
        # that claim and its declared monitor, the declared pair's
        # `state_file` persistence the honest basis is cut from, and
        # the live run tick the staged skew is measured from. Each
        # absence is the release predating the substrate.
        converged = rig.converge(failures)
        incumbent_token = failover.owner_token(rig.duty_preamble)
        if incumbent_token is None:
            raise Inconclusive(
                "the field owner's startup log carries no "
                "write-ownership claim line — the pinned release "
                "predates the startup-claim record the leg's "
                "attribution reads",
                f"the duty preamble was {rig.duty_preamble}",
            )
        basis_source = rig.standby_files.get("state_file")
        if basis_source is None:
            raise Inconclusive(
                "the manifest's declared standby declares no "
                "state_file — the run has no persisted basis to "
                "stage the skewed claim on"
            )
        if not os.path.exists(basis_source):
            raise Inconclusive(
                "the declared standby's persisted checkpoint is "
                "absent — the run has no honest basis to skew",
                f"the declared state file {basis_source} does not "
                "exist",
            )
        own_tick = converged["duty_role"].get("tick")
        if not isinstance(own_tick, int):
            raise Inconclusive(
                "the field owner serves no integer run tick — the "
                "pinned release predates the tick-domain contract "
                "the skew is staged on",
                f"GET /role answered {converged['duty_role']}",
            )
        evidence["converged"] = own_tick
        evidence["incumbent_token"] = incumbent_token

        # Phase 2 — the inside-bound claim resolves normally: the
        # claim-status observation names the standing claim's owner
        # and its declared monitor (the attribution the attempt is
        # judged against), a claim asserted on the line's own basis —
        # the incumbent's own owner token — joins the standing claim
        # under the recorded shared hold, a mutation lands under that
        # hold, and the release leaves the claim standing on the
        # incumbent. The arbitration resolves claims on the line's own
        # position, so the skewed attempt's refusal below is
        # attributable to the skew rather than to a claim path that
        # refuses everything.
        standing = claim_owner(probe_io)
        evidence["standing"] = standing
        if not claim_reclaim.mutation_fenced(standing):
            raise Inconclusive(
                "a third attachment's claim-status observation met "
                "no fencing verdict — the pinned release predates "
                "the claim-status surface the leg's attribution "
                "reads",
                f"the claim-status probe answered {standing}",
            )
        if claim_reclaim.verdict_owner(standing) != incumbent_token:
            failures.append(
                "the standing claim names owner "
                f"{claim_reclaim.verdict_owner(standing)}, not the "
                f"launched field owner's token {incumbent_token} — "
                "the pair is not in its launch claim state"
            )
            raise Abort
        if stranded_rejoin.verdict_monitor(standing) is None:
            raise Inconclusive(
                "the standing claim's verdict declares no monitor — "
                "the pinned release predates the claim-declared "
                "monitor the leg's attribution reads",
                f"the claim-status probe answered {standing}",
            )
        joined = probe_io.request(
            {"op": "ensure_writer", "owner": incumbent_token}
        )
        evidence["joined"] = joined
        if claim_reclaim.unsupported_verb(joined):
            raise Inconclusive(
                "the claim-lifecycle verbs are answered "
                "invalid_request — the pinned release predates the "
                "claim verbs the inside-bound control rides on",
                f"ensure_writer answered {joined}",
            )
        if joined.get("result") != "claimed_shared" or joined.get(
            "owner"
        ) != incumbent_token:
            failures.append(
                "a claim asserted on the line's own basis answered "
                f"{joined} — expected claimed_shared under the "
                f"incumbent's token {incumbent_token}"
            )
            raise Abort
        held_probe = claim_owner(probe_io)
        mutation = probe_io.request({"op": "step", "dt": 0})
        evidence["mutation"] = mutation
        if held_probe.get("result") != "done" or mutation.get(
            "result"
        ) != "stepped":
            failures.append(
                "a mutation under the inside-bound claim's shared "
                f"hold answered {mutation} (probe {held_probe}) — an "
                "inside-bound claim must resolve normally and serve "
                "the field"
            )
            raise Abort
        released = probe_io.request({"op": "release_writer"})
        after = claim_owner(probe_io)
        evidence["released"] = released
        if released.get("result") != "done" or (
            claim_reclaim.verdict_owner(after) != incumbent_token
        ):
            failures.append(
                "the inside-bound holder's release left the claim at "
                f"{after} (release {released}) — the standing claim "
                "must keep fencing the field on the incumbent's own "
                "token"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "inside-bound",
                "probe": "fenced",
                "join": "claimed_shared",
                "mutation": "landed",
                "release": "claim-held",
                "bound": RECORDED_BOUND,
            }
        )

        # The journal floors the attempt must leave untouched — read
        # once, before the skewed seat asks for the field.
        floors = [
            (
                url,
                name,
                len(pair.get(f"{url}/journal", "GET /journal", failures)),
            )
            for url, name in (
                (duty_url, duty_decl["name"]),
                (standby_url, standby_decl["name"]),
            )
        ]

        # Phase 3 — the staged skew: the declared standby's own
        # persisted checkpoint copied onto a run tick past the live
        # line's by the staged overrun, then a third controller
        # launched on the pair's born-active shape at the deployed
        # plant with that basis — the pairless duplicate seat a
        # manifest admits, coming up on a clock past the recorded
        # bound.
        skew_path, seeded = skew_basis(basis_source, scratch, SKEW_TICKS)
        journal_path = os.path.join(scratch, "skewed-journal.jsonl")
        evidence["seeded"] = seeded
        process, claimant_url, preamble = born_active_failure.spawn_born_active(
            args,
            rig.plant_addr,
            {"state_file": skew_path, "journal_file": journal_path},
        )
        claimant = process
        evidence["claimant_preamble"] = preamble
        landed = resumed_tick(preamble)
        if landed is None:
            raise Inconclusive(
                "the skewed basis never resumed — the pinned release "
                "predates the restored-basis claim this leg skews",
                f"the launch reported {preamble}",
            )

        # The pair keeps scanning while the claimant asks — the
        # incident is watched on a live line, never on a frozen one.
        for _ in range(WINDOW_TICKS):
            rig.tick(standby_url, duty_url, failures)

        if claimant_url is None:
            # The refusal landed at activation and settled the launch:
            # the whole disposition is the process's own last words.
            try:
                code = process.wait(timeout=EXIT_WAIT)
            except Exception:
                code = None
            lines = preamble + trailing_stderr(process)
            evidence["claimant_exit"] = code
            evidence["claimant_output"] = " ".join(lines)
            if code is None:
                failures.append(
                    "the skewed claimant never ended inside the "
                    "bound — a refused claim is the launch's "
                    f"disposition: {preamble}"
                )
                raise Abort
            disposition = refusal_disposition(lines, code, failures)
        else:
            # A release that stands the refusal down instead of ending
            # on it: the pending surface, driven scan by scan, never
            # reaching the field. A seat that claims the field is the
            # preemption this contract closed.
            served = []
            for _ in range(WINDOW_TICKS):
                if process.poll() is not None:
                    break
                claim_reclaim.try_scan(claimant_url)
                report = claim_reclaim.try_role(claimant_url)
                if report is None:
                    break
                served.append(
                    f"{report.get('role')}/"
                    f"{stranded_rejoin.sync_kind(report)}"
                )
                if report.get("role") in ("promoting", "active"):
                    break
            evidence["served"] = served
            if any(
                entry.split("/")[0] in ("promoting", "active")
                for entry in served
            ):
                raise Inconclusive(
                    "the pinned release predates the claim-skew "
                    "bound — the skewed claimant claimed the field "
                    "and serves active",
                    f"its role walk was {served}",
                )
            code = process.poll()
            if code is not None:
                # It ended inside the window: the same own-record
                # judgment the activation-time refusal takes.
                try:
                    code = process.wait(timeout=EXIT_WAIT)
                except Exception:
                    code = None
                disposition = refusal_disposition(
                    preamble + trailing_stderr(process), code, failures
                )
            elif served:
                disposition = "bounded-pending"
            else:
                failures.append(
                    "the skewed claimant served no pending surface at "
                    "all — a claim that neither lands nor stands down "
                    "leaves an operator no verdict to read"
                )
                raise Abort

        # The staging's own arithmetic: the resumed basis must sit past
        # the recorded bound, or the leg has staged no skew and has no
        # claim to referee.
        if landed <= own_tick + RECORDED_BOUND:
            raise Inconclusive(
                "the staging accrued no overrun past the recorded "
                "bound — the pinned release predates the resumed "
                "basis this leg skews",
                f"the claimant resumed at tick {landed} against the "
                f"live line's {own_tick}",
            )
        overrun = landed - own_tick
        evidence["resumed"] = landed

        # The refusal, refused or bounded by name: the claimant's own
        # record names the live claim it met.
        observed = durable_observed(journal_path)
        evidence["observed"] = observed
        evidence["durable"] = sorted(durable_kinds(journal_path))
        if not observed:
            failures.append(
                "the skewed claimant's durable record carries no "
                "observed claim — the arbitration it met named no "
                "incumbent to refuse it"
            )
            raise Abort
        if incumbent_token not in observed:
            failures.append(
                f"the skewed claimant observed claims {observed}, "
                "never the live incumbent's own token "
                f"{incumbent_token} — its claim was arbitrated "
                "against something else"
            )
            raise Abort

        # The doctored negative: a leg asserting the skewed claim took
        # write-ownership from the live incumbent — the preemption the
        # recorded bound closes — must report the named diagnostic on
        # the honest refusal rather than passing unexercised.
        if tamper == "expect-preempt":
            failures.append(
                f"{TAMPER_EVIDENCE} from the live incumbent — the "
                f"honest run kept the claim on token {incumbent_token} "
                f"and {disposition} the skewed attempt"
            )
            raise Abort

        # The incumbent keeps the field: the claim-status observation
        # still names its own token and its declared monitor, and the
        # pair settled undisturbed across the window.
        held = claim_owner(probe_io)
        evidence["held"] = held
        if claim_reclaim.verdict_owner(held) != incumbent_token:
            raise Inconclusive(
                "the pinned release predates the claim-skew bound — "
                "the skewed claim took write-ownership from the live "
                "incumbent",
                f"the claim now stands under "
                f"{claim_reclaim.verdict_owner(held)}, not the "
                f"incumbent's token {incumbent_token}: {held}",
            )
        if stranded_rejoin.verdict_monitor(held) is None:
            raise Inconclusive(
                "the standing claim's verdict declares no monitor — "
                "the pinned release predates the claim-declared "
                "monitor the leg's attribution reads",
                f"the claim-status probe answered {held}",
            )
        if not pair_undisturbed(
            rig, floors, failures, "across the skewed attempt"
        ):
            raise Abort
        kinds = durable_kinds(journal_path)
        if disposition == "refused-by-name" and kinds and (
            "startup_claim_refused" not in kinds
        ):
            # The durable refusal record: where the launched run
            # declared a journal sink and that sink carries the run's
            # own records, the refusal it settled on is journaled
            # beside the observed claim — the named verdict an
            # operator reads after the fact, not only the last words
            # of a process that has ended.
            raise Inconclusive(
                "the pinned release predates the claim-skew bound's "
                "durable disposition — the refusal settled without "
                "its journaled record",
                f"the claimant's durable kinds were {sorted(kinds)}",
            )
        digest_entries.append(
            {
                "phase": "skew",
                "bound": RECORDED_BOUND,
                "skew": SKEW_TICKS,
                "own": own_tick,
                "resumed": landed,
                "overrun": overrun,
            }
        )
        digest_entries.append(
            {
                "phase": "attempt",
                "disposition": disposition,
                "claim": "incumbent-held",
                "observed": "incumbent-named",
                "pair": "undisturbed",
            }
        )

        # The clean pair settles, tracks, and promotes: the declared
        # pair rests on its launch roles, then runs the documented
        # switch — `POST /demote` on the field owner, `POST /promote`
        # on the converged standby — the field's write-ownership claim
        # moving to the promoted peer and the demoted peer settling
        # back on `tracking`.
        for _ in range(SETTLE_TICKS):
            rig.tick(standby_url, duty_url, failures)
        if not driver_recovery.roles_hold(
            rig, failures, "after the skewed attempt"
        ):
            raise Abort
        switched = rig.switch(duty_url, standby_url, failures)
        promoted = claim_owner(probe_io)
        evidence["promoted_claim"] = promoted
        if claim_reclaim.verdict_owner(promoted) == incumbent_token:
            failures.append(
                "the documented switch left write-ownership on the "
                "demoted owner — the promoted peer never claimed the "
                f"field: {promoted}"
            )
            raise Abort
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if standby_role.get("role") != "active":
            failures.append(
                "the promoted peer does not serve active after the "
                f"switch: {standby_role}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "switch",
                "ticks": switched["ticks"],
                "demoted": switched["demoted_role"].get("role"),
                "promoted": switched["promoted_role"].get("role"),
                "claim": "promoted-peer",
            }
        )

        # The restore: the documented switch back — the reconverged
        # ex-owner's promote answered `promoting` — seating the launch
        # roles and the launch owner's claim for the legs behind this
        # one.
        restored = rig.switch(
            standby_url,
            duty_url,
            failures,
            demote_what="the promoted standby",
            promote_what="the converged ex-owner",
        )
        if not driver_recovery.roles_hold(
            rig, failures, "after the restore switch"
        ):
            raise Abort
        reseated = claim_owner(probe_io)
        evidence["reseated_claim"] = reseated
        if claim_reclaim.verdict_owner(reseated) != incumbent_token:
            failures.append(
                "the restore switch did not re-seat write-ownership "
                "on the launch owner — the pair ends on another "
                f"peer's claim: {reseated}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "restore",
                "ticks": restored["ticks"],
                "duty": "active",
                "standby": "tracking",
                "claim": "launch-owner",
            }
        )
        evidence["final_tick"] = restored["ticks"][-1]
    except Inconclusive:
        raise
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if claimant is not None:
            pair.stop(claimant)
        if probe_io is not None:
            probe_io.close()
        if rig is not None:
            rig.close()
        shutil.rmtree(scratch, ignore_errors=True)
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
        choices=["expect-preempt"],
        help="doctor the leg's expectation to the pre-contract shape "
        "— asserting the skewed claim took write-ownership from the "
        "live incumbent — so the pass must fail naming the refusal "
        "it observed",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = claim_skew_bound_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"claim-skew-bound: {TAMPER_EVIDENCE} from the live "
                "incumbent — an inconclusive run offers the "
                "doctored case no evidence"
            )
            return 1
        # The digest line carries only the stable reason — the run's
        # own verdicts, tokens, and endpoints report on stderr, where
        # two identical passes need not share them.
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"claim-skew-bound: inconclusive — {detail}")
        print(f"claim-skew-bound-digest inconclusive — {reason}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"claim-skew-bound: {line}")
        return 1
    for failure in failures:
        eprint(f"claim-skew-bound: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"claim-skew-bound: the {args.tamper} case passed "
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
        f"claim-skew-bound-digest {digest} — tracking by tick "
        f"{evidence['converged']}, an inside-bound claim resolved "
        "normally on the incumbent's own token, a claimant resuming "
        f"a basis {evidence['resumed'] - evidence['converged']} ticks "
        "past the live line was refused naming the claim it met and "
        "the --standby remedy, and the clean pair promoted and "
        f"restored its launch roles to tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())