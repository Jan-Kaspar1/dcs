#!/usr/bin/env python3
"""The claim-monitor rendezvous leg for the reference plant — the
consumer-side proof that a field-owning controller's write-ownership
claim declares a *dialable* monitor endpoint under the manifest's
declared wildcard listen binds, so the field-arbitrated rendezvous
decision 101 records fires on the deployment shape it exists to
serve (WW-ENG-003, WW-LCM-001 — the #1135/#1166
claimed-monitor wildcard normalization the in-workspace
reproduction and the qa rig's wildcard-bound stranded-standby leg
pin, mirrored at the customer boundary).

The stranded-rejoin leg (`ci/legs/stranded_rejoin.py`) proves the
claim-declared monitor resolves a fenced demotion's re-join under
the harness's loopback binds; the demote-reconvergence leg proves
the announced-source adoption under the manifest's declared
`0.0.0.0` binds. This leg pins the remaining corner — the
declaration itself under the wildcard bind: a claimant bound to
`0.0.0.0` — every documented container launch — declares its *bind*
address, which a fenced peer would dial as its own loopback. Before
the contract the plant stored the wildcard verbatim, so the verdict
named a monitor no peer could reach and the rendezvous never fired
on exactly the deployment shape it exists to serve. The
normalization substitutes the claiming connection's proven source
for the unspecified declared IP — the same substitute a `?peer=`
wildcard announce resolves to — so the stored monitor is never
unspecified while a routable declaration stands verbatim. The rig
is the pair legs' shared one — `pair.launch_pair` under its
declared-binds launch binds each controller's `--listen` on its
manifest-declared host with a runner-assigned port, the defective
deployment's own shape. The run:

- convergence — the driven-tick loop converges the declared
  standby to `tracking`, each peer's verbatim wildcard bind
  asserted against its declared listen host;
- the gate — a third-party mutation probe through the dedicated
  plant connection answers the fencing verdict; the claim's
  declared monitor must name the field owner's own dialable
  endpoint and answer its monitor surface — never the undialable
  wildcard verbatim, never a foreign address;
- the claim-path demotion — `POST /promote` on the converged
  standby preempts the live claim, so the superseded owner's first
  fenced write demotes it in place with no demote-request
  boundary — the involuntary path whose only provable rendezvous is
  the standing claim's declared monitor;
- the rendezvous — the successor's claim again declares a dialable
  monitor; the demoted peer resolves the claim-declared successor,
  journals its attributed `field_claim_lost` and the
  `tracking_source_adopted` naming the declared endpoint — served
  and durable — and reconverges to one active plus one tracking
  standby, holding it across a driven pull train;
- the restore — the documented switch seats the launch owner back
  on the field, its claim again declaring its dialable monitor,
  and the pair's launch roles stand with each durable journal
  carrying the run's own role transitions under the single
  cold-start boundary.

The contract postdates the pinned release line: where the launched
tooling predates it — the launched active recording no owner-token
claim line, the manifest's pair carrying no journal files, a
fencing verdict naming no standing owner or declaring no monitor,
the claim declaring its wildcard bind address verbatim (the stored
monitor the normalization removes), a checkpoint without the
`source_owns_field`/`line_owner` stamps, or the journaled records
carrying no claimant — the run's own evidence is the pre-contract
shape and the leg reports `claim-monitor-rendezvous-digest
inconclusive` rather than asserting until the manifest repins a
release carrying the contract.

Usage:

    claim_monitor_rendezvous.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `claim-monitor-rendezvous-digest <sha256>` line
prints — the check runs two passes and compares them
(`claim-monitor-rendezvous-nondeterministic`). A contract violation
reports `claim-monitor-rendezvous: …` lines on stderr and exits 1 —
the check's `claim-monitor-rendezvous-failed`. `--tamper
expect-wildcard` doctors the leg's own expectation to the defect
shape — asserting the undialable wildcard monitor as
rendezvous-capable — so the leg proves its dialability assertion
fires on the honest normalized declaration rather than passing an
unexercised contract.
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import claim_reclaim
import demote_reconvergence
import failover
import pair
import simulate
import stranded_rejoin


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the undialable wildcard
# monitor — the verbatim declaration the defect stored, which
# every fenced peer dials as its own loopback — as
# rendezvous-capable must surface the named diagnostic on the
# honest normalized declaration rather than passing an
# unexercised contract.
LEG = {
    "order": 510,
    "title": "the claim-monitor rendezvous leg",
    "passes": "claim-monitor-rendezvous",
    "tampers": [
        {
            "name": "expect-wildcard",
            "passed": "an expect-wildcard case passed the claim-monitor-rendezvous leg",
            "missed": "the expect-wildcard case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted the undialable wildcard monitor"],
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
    reported on stderr only, where ephemeral verdicts, endpoints,
    and minted tokens belong."""


# The driven-scan bounds the episode's phases run: the superseded
# owner's demote-in-place settles inside a couple of scans, the
# re-join through the claim-declared monitor is the bounded window
# the contract names — the defect's indefinite strand is the
# failure the bound catches — and the pull train spans a handful
# of driven pulls; the restore's reconvergence rides the shared
# harness's own handover bound.
WATCH_SCANS = 6
REJOIN_SCANS = 12
HOLD_TICKS = 4


def addr(url):
    """The peer's dialable monitor address as `host:port` — the form
    the normalized claim declaration names once the wildcard bind
    resolves to the claim connection's proven source."""
    return url.removeprefix("http://")


def monitor_kind(declared, owner_addr):
    """Classify a claim-declared monitor: `wildcard` for an
    unspecified bind address — the verbatim `0.0.0.0` declaration
    the defective release stored, which every fenced peer dials as
    its own loopback — `owner` for the claiming peer's own dialable
    monitor, `foreign` for anything else. Only `owner` is a
    contract answer."""
    text = str(declared)
    host = text.rsplit(":", 1)[0]
    if host in ("0.0.0.0", "::", "[::]"):
        return "wildcard"
    if text == owner_addr:
        return "owner"
    return "foreign"


def audit_declared(verdict, label, owner_addr, failures, tamper):
    """Audit a fencing verdict's declared monitor: the contract's
    rendezvous candidate must be the claiming peer's own dialable
    endpoint — never the undialable wildcard the defect stored
    verbatim, never a foreign address — and must answer the monitor
    surface it names. A verdict carrying no monitor, or one
    declaring the wildcard verbatim, is the release predating the
    contract. Under `expect-wildcard` the leg's own expectation is
    doctored to the defect's shape — the undialable declaration
    asserted rendezvous-capable — so a healthy normalized claim
    reports the named diagnostic. Returns the digest's kind word."""
    declared = stranded_rejoin.verdict_monitor(verdict)
    if declared is None:
        raise Inconclusive(
            f"the {label} fencing verdict declares no monitor — "
            "the pinned release predates the claim-declared-monitor "
            "contract the rendezvous resolves",
            f"the {label} fencing verdict was {verdict}",
        )
    kind = monitor_kind(declared, owner_addr)
    if tamper == "expect-wildcard":
        if kind != "wildcard":
            failures.append(
                "the doctored expectation wanted the undialable "
                "wildcard monitor declared as the rendezvous — "
                f"the {label} claim declared the {kind} endpoint "
                f"{declared}"
            )
        return kind
    if kind == "wildcard":
        raise Inconclusive(
            f"the {label} claim declares its wildcard bind "
            "verbatim — the pinned release predates the "
            "claim-monitor normalization the rendezvous needs",
            f"the {label} claim declared {declared} in {verdict}",
        )
    if kind != "owner":
        failures.append(
            f"the {label} claim declares monitor {declared} — a "
            "foreign endpoint, not the claiming peer's dialable "
            f"monitor {owner_addr}"
        )
        return kind
    # The declaration must be dialable: the named endpoint must
    # answer its own monitor surface, the liveness half of the
    # rendezvous the wildcard verbatim could never serve.
    try:
        simulate.http(f"http://{declared}/role")
    except Exception as error:
        failures.append(
            f"the {label} claim-declared monitor {declared} does "
            f"not answer its own role surface ({error}) — the "
            "declared rendezvous is undialable"
        )
    return kind


def claim_monitor_rendezvous_pass(args, tamper):
    """The claim-monitor rendezvous run: launch the declared pair on
    its declared wildcard binds, converge, gate the declared
    monitor's dialability through the dedicated plant connection,
    demote the field owner through the claim path, assert the
    demoted peer's claim-declared rendezvous with its journaled
    adoption evidence, and restore the launch roles. Returns
    `(digest_entries, evidence, failures)`; raises `Inconclusive`
    where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "claim-monitor rendezvous leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    rig = verdict_io = None
    try:
        rig = pair.launch_pair(args, declared, declared_binds=True)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_addr, standby_addr = addr(duty_url), addr(standby_url)
        duty_name, standby_name = duty_decl["name"], standby_decl["name"]
        # The claim observations the episode reads: the rig's own
        # client stays read-only while `verdict_io` runs the
        # third-party mutation probes whose fencing verdicts carry
        # the standing claim's declared monitor.
        verdict_io = simulate.PlantClient(rig.plant_addr)

        # Phase 1 — the declared wildcard binds deployed verbatim —
        # the defective deployment's own shape: each peer's claim
        # declares the wildcard bind its --listen bound unless the
        # normalization substitutes the claim connection's proven
        # source.
        binds = []
        for name, entry, bound in (
            (duty_name, duty_decl, rig.duty_bound),
            (standby_name, standby_decl, rig.standby_bound),
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

        # Phase 2 — convergence and the contract surface: the
        # launched active's recorded claim token, the standing
        # claim fencing third-party mutations while naming its
        # owner, and — the leg's first contract assertion — the
        # claim's declared monitor resolving the owner's own
        # dialable endpoint under the wildcard bind. Each absence is
        # the release predating the contract, never a violation of
        # it.
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
        owner_token = failover.owner_token(rig.duty_preamble)
        if owner_token is None:
            raise Inconclusive(
                "the launched active recorded no claim line — the "
                "pinned release claims only on promotion, predating "
                "the claim lifecycle the rendezvous contract "
                "rides on"
            )
        if rig.duty_files.get("journal_file") is None or (
            rig.standby_files.get("journal_file") is None
        ):
            raise Inconclusive(
                "the manifest's pair declares no journal files — "
                "the durable half of the adoption audit is absent"
            )
        probe0 = verdict_io.request({"op": "step", "dt": 0})
        evidence["probe0"] = probe0
        if not claim_reclaim.mutation_fenced(probe0):
            failures.append(
                "the field held no writer claim after convergence "
                f"— a third-party probe answered {probe0}, so the "
                "leg has no standing claim to read"
            )
            raise Abort
        if claim_reclaim.verdict_owner(probe0) is None:
            raise Inconclusive(
                "the fencing verdict names no standing owner — the "
                "pinned release predates the verdict attribution "
                "the contract's claimants read",
                f"the standing fencing verdict was {probe0}",
            )
        if claim_reclaim.verdict_owner(probe0) != owner_token:
            failures.append(
                "the standing claim names a foreign token, not the "
                f"launch owner — the pair is not in its launch "
                f"claim state: {probe0}"
            )
            raise Abort
        kind = audit_declared(
            probe0, "standing", duty_addr, failures, tamper
        )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "gate",
                "probe": "fenced",
                "owner": "named",
                "monitor": kind,
            }
        )

        # Phase 3 — the claim-path demotion: `POST /promote` on the
        # converged standby preempts the live claim — never `POST
        # /demote` on the owner first — so the superseded owner's
        # first fenced write demotes it in place with no
        # demote-request boundary: the involuntary entry whose only
        # provable rendezvous is the standing claim's declared
        # monitor.
        floor = len(
            pair.get(f"{duty_url}/journal", "GET /journal", failures)
        )
        status, report = pair.request(f"{standby_url}/promote", {})
        evidence["promote"] = {"status": status, "body": report}
        if status != 200 or report.get("role") != "promoting":
            failures.append(
                f"POST /promote on the converged standby answered "
                f"{status} {report} — the claim-path takeover must "
                "answer a promoting report"
            )
            raise Abort
        walk = []
        settled = None
        for _ in range(WATCH_SCANS):
            # The promoted peer's scan lands its claim — declaring
            # its monitor — then the superseded owner's next scan
            # fences its write and demotes it in place.
            pair.scan(standby_url, failures)
            pair.scan(duty_url, failures)
            report = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            walk.append(report.get("role"))
            if report.get("role") == "standby":
                settled = report
                break
        evidence["demotion"] = walk
        if settled is None:
            failures.append(
                "the superseded peer never demoted in place — the "
                f"claim-path takeover left its role walk at {walk}"
            )
            raise Abort

        # The successor's declared monitor — the field-arbitrated
        # rendezvous the demoted peer resolves — on a fresh
        # third-party probe.
        probe1 = verdict_io.request({"op": "step", "dt": 0})
        evidence["probe1"] = probe1
        if not claim_reclaim.mutation_fenced(probe1):
            failures.append(
                "the promoted peer's claim does not fence "
                f"third-party mutations: {probe1}"
            )
            raise Abort
        if claim_reclaim.verdict_owner(probe1) is None:
            raise Inconclusive(
                "the post-takeover fencing verdict names no "
                "standing owner — the pinned release predates the "
                "verdict attribution the contract reads",
                f"the post-takeover fencing verdict was {probe1}",
            )
        kind = audit_declared(
            probe1, "successor", standby_addr, failures, tamper
        )
        if failures:
            raise Abort
        declared_monitor = stranded_rejoin.verdict_monitor(probe1)

        # The journaled loss the demotion owes — exactly one
        # `field_claim_lost` attributed to the promoted peer's
        # claim — beside the fenced demote walk.
        journal = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        added = journal[floor:]
        losses = stranded_rejoin.lost_entries(added)
        evidence["losses"] = losses
        if not losses:
            failures.append(
                "the demoted peer journaled no field_claim_lost "
                "during the claim-path takeover"
            )
            raise Abort
        if len(losses) != 1:
            failures.append(
                f"the demoted peer journaled {len(losses)} "
                "field_claim_lost records for one takeover"
            )
            raise Abort
        if "claimant" not in losses[0][1]:
            raise Inconclusive(
                "the journaled field_claim_lost carries no "
                "claimant — the pinned release predates the loss "
                "attribution the episode reads",
                f"the demoted peer's field_claim_lost records were {losses}",
            )
        if losses[0][1].get("claimant") != claim_reclaim.verdict_owner(
            probe1
        ):
            failures.append(
                "the journaled field_claim_lost attributes the "
                f"takeover to {losses[0][1].get('claimant')}, not "
                "the promoted peer's claim token: "
                f"{losses}"
            )
            raise Abort
        role_walk = stranded_rejoin.role_walk(added)
        if any(origin is None for _frm, _to, origin in role_walk):
            raise Inconclusive(
                "the demoted peer's role walk carries no switch "
                "origin — the pinned release predates the fenced "
                "attribution the episode reads",
                f"the demoted peer's journaled role walk was {role_walk}",
            )
        if role_walk != [
            ("active", "demoting", "fenced"),
            ("demoting", "standby", "fenced"),
        ]:
            failures.append(
                "the demoted peer's journaled role walk is "
                f"{role_walk}, expected the fenced "
                "active → demoting → standby"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "takeover",
                "walk": walk,
                "monitor": kind,
                "loss": "attributed",
            }
        )

        # Phase 4 — the rendezvous: the demoted peer resolves the
        # standing claim's declared monitor through the verified
        # tracking path and reports `tracking` inside the bound —
        # the indefinite strand the defect left is the failure this
        # bound catches.
        rejoin = [stranded_rejoin.sync_kind(settled)]
        tracked = settled if claim_reclaim.tracking(settled) else None
        for _ in range(REJOIN_SCANS):
            if tracked is not None:
                break
            pair.scan(standby_url, failures)
            pair.scan(duty_url, failures)
            report = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            rejoin.append(stranded_rejoin.sync_kind(report))
            if claim_reclaim.tracking(report):
                tracked = report
                break
        evidence["rejoin"] = rejoin
        if tracked is None:
            failures.append(
                "the demoted peer never re-joined through the "
                "claim-declared monitor — the undialable-rendezvous "
                f"strand the contract closed; its sync stayed {rejoin}"
            )
            raise Abort

        # The adoption evidence: the demoted peer journaled exactly
        # one `tracking_source_adopted` naming the endpoint the
        # standing claim declared — served and durable alike — and
        # the re-joined line document carries the honest stamps
        # naming the promoted peer.
        journal = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        adoptions = stranded_rejoin.adopted_entries(journal[floor:])
        sources = [record.get("source") for _seq, record in adoptions]
        evidence["adopted"] = sources
        if len(adoptions) != 1:
            failures.append(
                f"the demoted peer journaled {len(adoptions)} "
                "tracking_source_adopted records for one "
                f"rendezvous: {sources}"
            )
        elif str(sources[0]) != str(declared_monitor):
            failures.append(
                f"the adopted source {sources[0]} is not the "
                "claim-declared successor the standing claim named "
                f"({declared_monitor})"
            )
        durable_sources = demote_reconvergence.adopted_sources(
            rig.duty_files["journal_file"]
        )
        evidence["durable_adopted"] = durable_sources
        if str(declared_monitor) not in [
            str(source) for source in durable_sources
        ]:
            failures.append(
                "the durable journal carries no adoption naming "
                f"the claim-declared monitor {declared_monitor}: "
                f"{durable_sources}"
            )
        doc = pair.get(
            f"{duty_url}/checkpoint", "GET /checkpoint", failures
        )
        evidence["checkpoint"] = doc
        if (
            "source_owns_field" not in doc
            or "line_owner" not in doc
        ):
            raise Inconclusive(
                "the re-joined peer serves a checkpoint without "
                "the field-ownership stamps — the pinned release "
                "predates the rendezvous contract",
                f"the re-joined peer's checkpoint was {doc}",
            )
        if doc.get("source_owns_field") is not False or not str(
            doc.get("line_owner")
        ).endswith(":" + stranded_rejoin.monitor_port(standby_url)):
            failures.append(
                "the re-joined peer serves a checkpoint stamping "
                f"source_owns_field={doc.get('source_owns_field')} "
                f"line_owner={doc.get('line_owner')} — expected the "
                "honest non-owning stamp naming the promoted peer "
                f"on :{stranded_rejoin.monitor_port(standby_url)}"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "rendezvous",
                "rejoin": rejoin,
                "adopted": "declared",
                "document": "stamped",
            }
        )

        # Phase 5 — the settled shape: exactly one active — the
        # promoted peer — with the demoted owner tracking it,
        # holding across a driven pull train.
        promoted_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        demoted_role = pair.get(
            f"{duty_url}/role", "GET /role", failures
        )
        if promoted_role.get("role") != "active":
            failures.append(
                f"the promoted peer reports "
                f"{promoted_role.get('role')!r}, expected active"
            )
            raise Abort
        if not claim_reclaim.tracking(demoted_role):
            failures.append(
                f"the demoted peer reports {demoted_role} — "
                "expected a tracking standby"
            )
            raise Abort
        held = []
        for _ in range(HOLD_TICKS):
            _tracked, _owner = rig.tick(
                duty_url, standby_url, failures
            )
            report = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            held.append(stranded_rejoin.sync_kind(report))
        if any(kind_ != "tracking" for kind_ in held):
            failures.append(
                "the re-joined peer dropped out of tracking "
                f"across the pull train: {held}"
            )
            raise Abort
        digest_entries.append(
            {"phase": "hold", "held": held}
        )

        # Phase 6 — the restore: the documented switch seats the
        # launch owner back on the field — the promoted peer
        # demoting onto its configured tracking source, the
        # reconverged member promoting — and the re-seated claim
        # again declares the owner's dialable monitor.
        restored = rig.switch(
            standby_url,
            duty_url,
            failures,
            demote_what="the promoted peer",
            promote_what="the reconverged duty member",
        )
        evidence["restored_at"] = restored["owner"]["tick"]
        post = verdict_io.request({"op": "step", "dt": 0})
        evidence["post_probe"] = post
        if not claim_reclaim.mutation_fenced(post):
            failures.append(
                "the restored claim does not fence third-party "
                f"mutations: {post}"
            )
            raise Abort
        if claim_reclaim.verdict_owner(post) != owner_token:
            failures.append(
                "the restored claim does not name the launch "
                f"owner's own token: {post}"
            )
            raise Abort
        kind = audit_declared(
            post, "restored", duty_addr, failures, tamper
        )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "restore",
                "demote": restored["demote"],
                "promote": restored["promote"],
                "ticks": restored["ticks"],
                "monitor": kind,
            }
        )

        # Phase 7 — the durable record: each peer's declared journal
        # file carrying its own role transitions under the single
        # cold-start boundary with `seq` order intact, the demoted
        # peer's adoption naming the claim-declared endpoint, and
        # each declared state file checkpointing the run's final
        # tick under the manifest's fingerprint.
        final_tick = restored["owner"]["tick"]
        expected = {
            duty_name: [
                ("active", "demoting"),
                ("demoting", "standby"),
                ("standby", "promoting"),
                ("promoting", "active"),
            ],
            standby_name: [
                ("standby", "promoting"),
                ("promoting", "active"),
                ("active", "demoting"),
                ("demoting", "standby"),
            ],
        }
        files = {
            duty_name: rig.duty_files,
            standby_name: rig.standby_files,
        }
        adoptions_expect = {
            duty_name: (standby_addr, True),
            standby_name: (duty_addr, False),
        }
        persisted = {}
        for name in (duty_name, standby_name):
            journal_path = files[name].get("journal_file")
            state_path = files[name].get("state_file")
            record = {}
            if journal_path is None or not os.path.exists(journal_path):
                failures.append(
                    f"{name}'s declared journal file {journal_path} "
                    "does not exist — the --journal-file flag was "
                    "not honored"
                )
            else:
                records = pair.journal_records(journal_path)
                boundaries = [
                    record_ for kind_, record_ in records
                    if kind_ == "boundary"
                ]
                if boundaries != [{"run": 1, "tick": 0}]:
                    failures.append(
                        f"{name}'s journal boundaries are "
                        f"{boundaries}, expected the single "
                        "cold-start marker — a restart boundary "
                        "inside the episode is a process restart"
                    )
                entries = [
                    record_ for kind_, record_ in records
                    if kind_ == "entry"
                ]
                seqs = [entry["seq"] for entry in entries]
                if seqs != list(range(1, len(seqs) + 1)):
                    failures.append(
                        f"{name}'s journal seqs are not 1..n in "
                        f"order: {seqs}"
                    )
                transitions = pair.role_transitions(entries)
                want = expected[name]
                if [
                    (frm, to) for _tick, frm, to in transitions
                ] != want:
                    failures.append(
                        f"{name}'s journal file carries the role "
                        f"transitions {transitions}, expected {want}"
                    )
                peer_addr, owed = adoptions_expect[name]
                sources = demote_reconvergence.adopted_sources(
                    journal_path
                )
                if owed and not sources:
                    failures.append(
                        f"{name}'s durable journal carries no "
                        "tracking_source_adopted — the "
                        "claim-declared rendezvous's adoption "
                        "went unrecorded"
                    )
                for source in sources:
                    if source != peer_addr:
                        failures.append(
                            f"{name}'s durable journal adopted a "
                            f"foreign source {source} — expected "
                            f"the peer's dialable monitor {peer_addr}"
                        )
                # The digest-safe record: the role walk and counts
                # — never the raw entries, whose `field_claim_lost`
                # carries the per-process minted claimant token two
                # identical passes cannot share.
                record["entries"] = len(entries)
                record["transitions"] = [
                    (frm, to) for _tick, frm, to in transitions
                ]
                record["adopted"] = [
                    "peer" if source == peer_addr else "foreign"
                    for source in sources
                ]
            if state_path is not None:
                if not os.path.exists(state_path):
                    failures.append(
                        f"{name}'s declared state file {state_path} "
                        "does not exist — the --state-file flag "
                        "was not honored"
                    )
                else:
                    try:
                        with open(state_path) as handle:
                            checkpoint = json.load(handle)
                    except (OSError, json.JSONDecodeError) as error:
                        failures.append(
                            f"{name}'s state file does not parse: "
                            f"{error}"
                        )
                        checkpoint = None
                    if checkpoint is not None:
                        if checkpoint.get("tick") != final_tick:
                            failures.append(
                                f"{name}'s state file persisted "
                                f"tick {checkpoint.get('tick')} "
                                f"while the run stood at {final_tick}"
                            )
                        if (
                            checkpoint.get("model_fingerprint")
                            != rig.fingerprint
                        ):
                            failures.append(
                                f"{name}'s state file carries "
                                f"fingerprint "
                                f"{checkpoint.get('model_fingerprint')}"
                                f", the manifest declares "
                                f"{rig.fingerprint}"
                            )
                        record["state_tick"] = checkpoint.get("tick")
            persisted[name] = record
        if failures:
            raise Abort
        digest_entries.append(
            {"phase": "record", "persisted": persisted}
        )
        evidence["final_tick"] = final_tick
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        # Detach the claim attachment cleanly: release whatever
        # hold it still carries — a hold left standing keeps the
        # field claimed for a dead token — then close. The release
        # drops only the connection's own hold, so it never takes
        # the owner's claim down with it.
        if verdict_io is not None:
            try:
                verdict_io.request({"op": "release_writer"})
            except Exception:
                pass
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
        choices=["expect-wildcard"],
        help="doctor the leg's own expectation — asserting the "
        "undialable wildcard monitor as rendezvous-capable — so the "
        "pass must fail naming the honest normalized declaration",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = (
            claim_monitor_rendezvous_pass(args, args.tamper)
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "claim-monitor-rendezvous: the doctored expectation "
                "wanted the undialable wildcard monitor declared "
                "— an inconclusive run offers the doctored case no "
                "evidence"
            )
            return 1
        # The digest line carries only the stable reason — the
        # run's own verdicts and endpoints report on stderr, where
        # two identical passes need not share them.
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"claim-monitor-rendezvous: inconclusive — {detail}")
        print(
            "claim-monitor-rendezvous-digest inconclusive — "
            f"{reason}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"claim-monitor-rendezvous: {line}")
        return 1
    for failure in failures:
        eprint(f"claim-monitor-rendezvous: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"claim-monitor-rendezvous: the {args.tamper} case "
                "passed silently — the leg never noticed the "
                "doctored expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"claim-monitor-rendezvous-digest {digest} — tracking by "
        f"tick {evidence['converged']}, the wildcard-bound claim's "
        "declared monitor dialable, the claim-path demotion "
        "reconverged through the claim-declared successor, and "
        f"the launch roles restored at tick "
        f"{evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
