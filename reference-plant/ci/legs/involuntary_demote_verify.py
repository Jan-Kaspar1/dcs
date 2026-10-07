#!/usr/bin/env python3
"""The involuntary-demote-verify leg for the reference plant — the
consumer-side proof that a field owner demoted *involuntarily* — a
fenced write after its field claim was preempted, crossing no request
boundary — consumes its recorded `GET /checkpoint?peer=` announced set
only through lazy verification: a recorded hint is a verify candidate,
never a pull target (WW-ENG-003, WW-LCM-001 — decision 92's contract,
landed as the announced-source verify, mirrored at the customer
boundary).

The peer-announce leg (`ci/legs/peer_announce.py`) and the
announced-source-verify leg (`ci/legs/announced_source_verify.py`)
exercise the *request* boundary: `POST /demote` proves the recorded
hints before it demotes, preferring a verified field owner, and a
passing hint pins into `adopted` with a journaled
`tracking_source_adopted`. No leg covers the involuntary path, where no
boundary verify runs at all: the demotion happens on the owner's own
fenced write and the announced set is consumed from the tracking path
instead. This leg covers it on the customer's declared deployment, in
both postures the contract names, through the shared `launch_pair` rig:

- the keyed declared pair — the pair converges `active`/`tracking`, a
  foreign endpoint (`announced_source_verify.ForeignEndpoint`, the shared
  hostile-endpoint staging) records itself on the active through a
  `?peer=` announce, and the field claim is then preempted by the
  documented `POST /promote` on the tracking standby — no `POST /demote`
  first, so the owner's demotion is the fencing path's alone. Through
  the demoted peer's served surface and durable journal: its pulls never
  chase the foreign endpoint beyond the bounded verify pass, the refused
  probe is journaled by name (`tracking_source_refused`), no
  `tracking_source_adopted` names the foreign endpoint, a verified
  successor's endpoint pins and is journaled by name, the demotion walk
  is the attributed fenced `field_claim_lost` plus the
  `role_changed` entries with `origin: "fenced"`, and the pair
  reconverges to exactly one `active` plus one `tracking` standby;
- the unkeyed consumer seat — the inert-hint clause. The deployment's
  keyed pair can prove a hint; a member launched without the shared
  `--pair-token` cannot, and on such a run a bare hint is no tracking
  source at all. A second controller the leg spawns as an unkeyed
  sibling of the field owner takes the field through `POST /promote`,
  is announced at through the same foreign endpoint, and is then
  preempted in turn — so the unkeyed seat is the involuntary demotion's
  subject. Its verdict must be its own: the foreign endpoint's ledger
  records *no* pull at all (a bare hint on an unkeyed run earns not even
  a verify pass), the peer reports the `unsynchronized`/`orphaned`
  verdict rather than following the foreign document, nothing names the
  foreign endpoint in its adoptions, and the endpoint the field's own
  arbitration names — the promoted successor's declared monitor — is
  what it pins, journaled by name;
- the restore — the walk back leaves the manifest's declared launch
  roles standing: the duty controller `active`, the declared standby
  `tracking`.

The contract postdates some pinned release lines: where the launched
tooling predates it — a served checkpoint without the
`source_owns_field` stamp, the declared `journal_file` persistence
absent, a `POST /promote` the monitor does not route at all (the
harness admits no claim-preempt staging), or no durable
`tracking_source_refused` record at all — the leg reports
`involuntary-demote-verify-digest inconclusive` rather than
asserting until the manifest repins a release carrying the contract.

Usage:

    involuntary_demote_verify.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `involuntary-demote-verify-digest <sha256>` line prints
— the check runs two passes and compares them
(`involuntary-demote-verify-nondeterministic`). A contract violation
reports `involuntary-demote-verify: …` lines on stderr and exits 1 —
the check's `involuntary-demote-verify-failed`. `--tamper expect-adopted`
doctors the leg's expectation to the pre-contract shape — asserting the
involuntarily demoted peer adopted the foreign endpoint's announced
hint, the finding `involuntary-demote-unverified-announced-hint`
reported — so the leg proves its no-adoption audit fires on the honest
record rather than passing an unexercised contract.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import announced_source_verify
import claim_reclaim
import pair
import simulate
import tracking_source_fallback

ForeignEndpoint = announced_source_verify.ForeignEndpoint
forged_document = announced_source_verify.forged_document
refusals = announced_source_verify.refusals
adopted_ports = announced_source_verify.adopted_ports
adopted_sources = announced_source_verify.adopted_sources


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the involuntarily demoted peer
# adopted the foreign endpoint's announced hint must surface the named
# diagnostic on the honest no-adoption record rather than passing an
# unexercised contract.
LEG = {
    "order": 205,
    "title": "the involuntary-demote-verify leg",
    "passes": "involuntary-demote-verify",
    "tampers": [
        {
            "name": "expect-adopted",
            "passed": "an expect-adopted case passed the involuntary-demote-verify leg",
            "missed": "the expect-adopted case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the demoted peer to "
                "adopt the foreign endpoint's announced hint"
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
    product failure."""


# The bound on the verify pass the involuntary path spends at one hinted
# endpoint: `probe_announced_hints` takes newest-announcer first, one
# bounded pull per hint per pass, and re-probes an unchanged set only
# after the four-second `ANNOUNCED_VERIFY_RETRY` window. A hint followed
# as a pull target would be dialed once per scan instead.
VERIFY_PULL_BOUND = 4

# The unkeyed seat's pull budget at the foreign endpoint across the whole
# episode: the announced contract is keyed-only outright, so a bare hint
# on an unkeyed run earns not even a verify pull. The allowance is one
# pull — the ledger must read zero, and a nonzero count fails on its own
# count line before the bound is consulted.
UNKEYED_PULL_BOUND = 1

# The driven-scan bounds the two episodes run inside: a claim preemption
# lands its demote-in-place on the superseded owner's own fenced write,
# and the re-join that follows converges inside a handful of further
# pulls. The round count is the pessimistic bound — each round drives one
# scan on every launched peer, so a slow arbitration costs rounds, not
# correctness.
CONVERGE_SCANS = 12
DEMOTE_ROUNDS = 160
POLL_SLEEP = 0.02

# The sync verdicts a demoted peer may legitimately report: it followed
# a proven endpoint of this line. `degraded` is the wedge this contract
# exists to refuse — a stranded pull on the foreign endpoint.
CONVERGED_VERDICTS = ("tracking", "orphaned")

# The statuses the documented `POST /promote` answers on a serving
# monitor: the switch's own 200, or a 409 naming one of its refusals
# (`not_converged`, `already_active`, `no_tracking_source`). Anything
# else — an unrouted 404, a malformed-body 400 — is a release carrying
# no claim-preempt staging at all, which the leg reports inconclusive
# rather than as a preempt refusal it cannot read.
PREEMPT_ANSWERS = (200, 409)


def drive(urls, rounds, accept, failures, what):
    """One driven scan per launched peer per round until `accept` holds
    over the polls — the demote-in-place and re-join watches the
    involuntary episodes run. Returns `{'rounds', 'roles'}`, or None
    when the bound passes with the condition never met."""
    watched = []
    for index in range(rounds):
        for url in urls:
            pair.scan(url, failures)
        roles = [pair.get(f"{url}/role", "GET /role", failures) for url in urls]
        watched.append(roles)
        if accept(roles):
            return {"rounds": index + 1, "roles": roles}
    failures.append(
        f"{what} never settled within {rounds} driven rounds — the last "
        f"reports read {json.dumps(watched[-1])[:400]}"
    )
    return None


def role_of(report):
    """The role a RoleReport carries, or None."""
    return (report or {}).get("role")


def settle(urls, rounds, url, failures, what):
    """Drive the launched peers until the peer at `url` reports the
    tracking verdict — the re-join a switch owes its peers before the
    next documented request can be issued against them."""
    return drive(
        urls,
        rounds,
        lambda roles: claim_reclaim.tracking(
            pair.get(f"{url}/role", "GET /role", failures)
        ),
        failures,
        what,
    )


def actives(urls, failures):
    """The launched peers currently reporting the field-owning role."""
    return [
        url
        for url in urls
        if role_of(pair.get(f"{url}/role", "GET /role", failures))
        == "active"
    ]


def restore_roles(duty_url, standby_url, failures, rounds, what):
    """Walk the pair back onto its manifest-declared launch roles: the
    peer still holding the field demotes, and — a demotion releases the
    field's single-writer claim, which the declared duty controller's
    conditional claim then re-seats for itself — the walk promotes the
    duty controller only when it did not already re-own the field. The
    pair is left with exactly one field owner at `duty_url`."""
    both = [duty_url, standby_url]
    settled = lambda roles: len(actives(both, failures)) == 1
    for _ in range(3):
        standing = actives(both, failures)
        if standing == [duty_url]:
            return
        if standing:
            pair.request(f"{standing[0]}/demote", {})
            drive(both, rounds, settled, failures, what)
        else:
            pair.request(f"{duty_url}/promote", {})
            drive(both, rounds, settled, failures, what)
    standing = actives(both, failures)
    if standing != [duty_url]:
        failures.append(
            f"{what}: the pair could not be walked back onto its launch "
            f"roles — the field owners read {standing}"
        )


def journal_kinds(path, since):
    """`(kind, payload)` for every event the `--journal-file`'s records
    from index `since` carry — the durable half of the involuntary
    demotion's audit."""
    recorded = []
    for kind, entry in pair.journal_records(path)[since:]:
        if kind != "entry":
            continue
        for name, body in (entry.get("event") or {}).items():
            recorded.append((name, body))
    return recorded


def fenced_walk(path, since):
    """The `role_changed` walk the fencing path recorded — the
    `(from, to, origin)` triples in journal order. An involuntary
    demotion is the `active -> demoting -> standby` walk attributed to
    the peer's own protective demotion rather than to any request."""
    return [
        (body.get("from"), body.get("to"), body.get("origin"))
        for name, body in journal_kinds(path, since)
        if name == "role_changed"
    ]


def claim_losses(path, since):
    """The `field_claim_lost` records the supersede owed — each naming
    the claimant that took the field. The minted owner tokens are run
    nonces and stay out of the digest."""
    return [
        body
        for name, body in journal_kinds(path, since)
        if name == "field_claim_lost"
    ]


def episode_audit(
    url,
    journal_path,
    floor,
    foreign,
    successor_url,
    urls,
    want_refusal,
    failures,
    label,
):
    """The one episode's audit — the assertions both the keyed and the
    unkeyed involuntary demotion share. Returns the record the digest
    entry is built from: the demoted peer's served surface (its role, its
    sync verdict, the line document it now serves), the foreign
    endpoint's pull budget, and its journal's attributed demotion walk
    and named adoptions."""
    pull_floor = 0
    pulls = foreign.served_pulls(pull_floor)
    role = pair.get(f"{url}/role", "GET /role", failures)
    doc = pair.get(f"{url}/checkpoint", "GET /checkpoint", failures)
    kind = tracking_source_fallback.sync_kind(role)
    adoptions = adopted_ports(journal_path)
    refusals_here = [
        (source, detail)
        for source, detail in refusals(journal_path, floor)
        if str(source).endswith(":" + str(foreign.port))
    ]
    walk = fenced_walk(journal_path, floor)
    losses = claim_losses(journal_path, floor)
    record = {
        "role": role_of(role),
        "sync": kind,
        "line_owner": doc.get("line_owner"),
        "source_owns_field": doc.get("source_owns_field"),
        "verify_pulls": len(pulls),
        "adopted": [
            tracking_source_fallback.peer_kind(urls, source)
            for source in adopted_sources(journal_path)
        ],
        "walk": ["->".join(str(part) for part in step) for step in walk],
        "losses": len(losses),
    }
    if role_of(role) != "standby":
        failures.append(
            f"{label}: the demoted peer reports "
            f"{role_of(role)!r} — the fenced write never demoted it in "
            "place"
        )
    if kind not in CONVERGED_VERDICTS:
        failures.append(
            f"{label}: the demoted peer reports the {kind!r} verdict — a "
            "demotion that followed an unproven announced hint strands "
            "the peer instead of converging"
        )
    if doc.get("source_owns_field") is not True and not str(
        doc.get("line_owner")
    ).endswith(":" + tracking_source_fallback.monitor_port(successor_url)):
        # A demoted peer's served document stamps itself non-owning —
        # the successor's monitor address is the line-ownership stamp
        # the re-join is proven by, and a hint adopted instead leaves
        # the stamp absent or foreign.
        failures.append(
            f"{label}: the demoted peer serves a checkpoint that neither "
            "claims the field nor propagates its line owner — it is not "
            f"this line's continuation: {doc.get('source_owns_field')!r} "
            f"{doc.get('line_owner')!r}"
        )
    if not str(doc.get("line_owner")).endswith(
        ":" + tracking_source_fallback.monitor_port(successor_url)
    ):
        failures.append(
            f"{label}: the demoted peer serves line_owner "
            f"{doc.get('line_owner')!r} where the promoted successor's "
            "monitor was expected — the pulls moved somewhere else"
        )
    if len(pulls) > VERIFY_PULL_BOUND:
        failures.append(
            f"{label}: the demoted peer spent {len(pulls)} pulls at the "
            "foreign endpoint — a recorded hint is a verify candidate, "
            "never a pull target"
        )
    if str(foreign.port) in adoptions:
        failures.append(
            f"{label}: the demoted peer journaled a "
            f"tracking_source_adopted naming the foreign endpoint's "
            f"port {foreign.port} — the announced hint was adopted"
        )
    if tracking_source_fallback.monitor_port(successor_url) not in adoptions:
        failures.append(
            f"{label}: the demoted peer journaled no "
            "tracking_source_adopted naming the promoted successor's "
            f"monitor — the adoptions are {adoptions}"
        )
    if ("active", "demoting", "fenced") not in walk or (
        "demoting",
        "standby",
        "fenced",
    ) not in walk:
        failures.append(
            f"{label}: the journaled role walk is {walk} — an involuntary "
            "demotion is the fenced active->demoting->standby walk, not "
            "an operator request's transitions"
        )
    if not losses:
        failures.append(
            f"{label}: the demotion journaled no field_claim_lost — the "
            "supersede is unaudited"
        )
    if want_refusal and not refusals_here:
        failures.append(
            f"{label}: the foreign endpoint's hint left no durable "
            "tracking_source_refused record — a refused probe is "
            "auditable, never silent"
        )
    if not want_refusal and refusals_here:
        failures.append(
            f"{label}: the foreign endpoint's hint left durable "
            f"tracking_source_refused records {refusals_here} — a bare "
            "hint on an unkeyed run earns no probe at all, so it can "
            "leave no served refusal behind either"
        )
    return record


def involuntary_demote_verify_pass(args, tamper):
    """The exercised run: converge the declared pair, record the foreign
    endpoint on the active, preempt the field claim so the active demotes
    involuntarily, and audit the demoted peer — then the same episode on
    an unkeyed seat, where the bare hint is inert outright. Returns
    `(digest_entries, evidence, failures)`; raises `Inconclusive` where
    the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "involuntary-demote-verify leg has nothing to exercise"
        )
    if announced_source_verify.rig_journal_absent(declared):
        raise Inconclusive(
            "the manifest's declared pair carries no journal_file — "
            "the durable no-adoption audit is absent"
        )
    _manifest, duty_decl, standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    rig = None
    foreign = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        urls = {
            duty_decl["name"]: duty_url,
            standby_decl["name"]: standby_url,
        }
        duty_journal = rig.duty_files["journal_file"]
        if duty_journal is None:
            raise Inconclusive(
                "the manifest's duty controller declares no journal_file "
                "— the durable no-adoption audit is absent"
            )
        converged = rig.converge(failures, count=CONVERGE_SCANS)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        own = pair.get(f"{duty_url}/checkpoint", "GET /checkpoint", failures)
        forged = forged_document(own)
        if forged is None:
            raise Inconclusive(
                "the field owner serves no field-owning checkpoint "
                "document — the pinned release predates the "
                f"announced-source contract the leg stages a forgery "
                f"of: {own}"
            )
        foreign = ForeignEndpoint(forged)
        served = foreign.announce(duty_url, failures)
        evidence["announced_at"] = served["tick"]

        # Phase 1 — the keyed declared pair's involuntary demotion: the
        # documented `POST /promote` on the tracking standby preempts the
        # field claim. Never `POST /demote` on the owner first — the
        # demotion must be the superseded owner's own fenced write.
        floor = len(pair.journal_records(duty_journal))
        status, body = pair.request(f"{standby_url}/promote", {})
        if status not in PREEMPT_ANSWERS:
            raise Inconclusive(
                f"POST /promote on the tracking standby answered {status} "
                f"{body} — the harness admits no claim-preempt staging, "
                "so the pinned release predates the verb the involuntary "
                "demotion rides on"
            )
        if status != 200:
            failures.append(
                f"POST /promote on the tracking standby answered {status} "
                f"{body} — the field claim could not be preempted"
            )
            raise Abort
        settled = drive(
            [duty_url, standby_url],
            DEMOTE_ROUNDS,
            lambda roles: role_of(roles[1]) == "active"
            and role_of(roles[0]) == "standby"
            and tracking_source_fallback.sync_kind(roles[0])
            in CONVERGED_VERDICTS,
            failures,
            "the keyed pair's involuntary demotion",
        )
        record = episode_audit(
            duty_url,
            duty_journal,
            floor,
            foreign,
            standby_url,
            urls,
            True,
            failures,
            "the keyed declared pair",
        )
        evidence["keyed"] = {"promote": {"status": status, "body": body},
                             "rounds": (settled or {}).get("rounds"),
                             "audit": record}
        if tamper == "expect-adopted":
            failures.append(
                "the doctored expectation wanted the demoted peer to "
                "adopt the foreign endpoint's announced hint — the "
                f"journaled adoptions read {record['adopted']}"
            )
        digest_entries.append(
            {
                "phase": "involuntary",
                "rounds": (settled or {}).get("rounds"),
                "audit": {
                    "role": record["role"],
                    "sync": record["sync"],
                    "source_owns_field": record["source_owns_field"],
                    "line_owner": tracking_source_fallback.peer_kind(
                        urls, record["line_owner"]
                    ),
                    "verify_pulls": record["verify_pulls"],
                    "adopted": record["adopted"],
                    "walk": record["walk"],
                    "losses": record["losses"],
                },
            }
        )
        if tamper == "expect-adopted":
            raise Abort

        # The first restore — the declared pair walked back onto its
        # manifest-declared launch roles, so the unkeyed episode starts
        # from the arrangement a customer deployment actually runs.
        restore_roles(
            duty_url,
            standby_url,
            failures,
            DEMOTE_ROUNDS,
            "the keyed episode's launch-role restore",
        )
        first_restore = rig.converge(failures, count=CONVERGE_SCANS)
        evidence["keyed_restored"] = first_restore["ticks"][-1]
        digest_entries.append(
            {
                "phase": "keyed-restore",
                "ticks": first_restore["ticks"],
                "duty_role": first_restore["duty_role"],
                "standby_role": first_restore["standby_role"],
            }
        )

        # Phase 2 — the unkeyed consumer pair's involuntary demotion:
        # the inert-hint clause. The announced contract is keyed-only
        # outright, so a bare `?peer=` hint on an unkeyed run is no
        # tracking source at all. The unkeyed shape is staged on its own
        # plant server with two controllers the leg spawns without the
        # shared `--pair-token` — a deployment whose pair carries no
        # tracking secret — so the declared keyed pair is never a witness
        # to the episode and the unkeyed peers' field claim is the only
        # one standing on that plant.
        unkeyed = unkeyed_url = unkeyed_peer = None
        unkeyed_plant = None
        scratch = tempfile.mkdtemp(prefix="dcs-unkeyed-", dir=rig.scratch)
        try:
            unkeyed_plant, unkeyed_plant_addr = pair.spawn_plant(
                args.plant_server, args.model, args.dynamics
            )
            owner_files = pair.persistence_files(
                scratch,
                {
                    "name": "unkeyed-owner",
                    "journal_file": "/var/tmp/owner-journal.jsonl",
                },
            )
            peer_files = pair.persistence_files(
                scratch,
                {
                    "name": "unkeyed-peer",
                    "journal_file": "/var/tmp/peer-journal.jsonl",
                },
            )
            unkeyed, unkeyed_url, preamble = pair.spawn_peer(
                args.controller,
                args.model,
                args.dt,
                unkeyed_plant_addr,
                None,
                owner_files,
            )
            if unkeyed_url is None:
                failures.append(
                    "the spawned unkeyed owner exited at startup: "
                    f"{'; '.join(preamble) or 'no diagnostic'}"
                )
                raise Abort
            unkeyed_peer, unkeyed_peer_url, preamble = pair.spawn_peer(
                args.controller,
                args.model,
                args.dt,
                unkeyed_plant_addr,
                unkeyed_url.removeprefix("http://"),
                peer_files,
            )
            if unkeyed_peer_url is None:
                failures.append(
                    "the spawned unkeyed peer exited at startup: "
                    f"{'; '.join(preamble) or 'no diagnostic'}"
                )
                raise Abort
            urls["unkeyed-owner"] = unkeyed_url
            urls["unkeyed-peer"] = unkeyed_peer_url
            unkeyed_journal = owner_files["journal_file"]
            unkeyed_settled = drive(
                [unkeyed_peer_url, unkeyed_url],
                DEMOTE_ROUNDS,
                lambda roles: role_of(roles[1]) == "active"
                and claim_reclaim.tracking(roles[0]),
                failures,
                "the unkeyed pair's convergence",
            )
            if unkeyed_settled is None:
                raise Abort
            evidence["unkeyed_converged"] = unkeyed_settled["rounds"]

            # The bare hint: the foreign endpoint stages the unkeyed
            # owner's own checkpoint replayed as a standby-shaped
            # document and records itself through the same `?peer=`
            # seam. Nothing about the endpoint changes between the keyed
            # and the unkeyed episode — only the pair's posture does.
            unkeyed_own = pair.get(
                f"{unkeyed_url}/checkpoint", "GET /checkpoint", failures
            )
            unkeyed_forged = forged_document(unkeyed_own)
            if unkeyed_forged is None:
                raise Inconclusive(
                    "the unkeyed owner serves no field-owning checkpoint "
                    "document — the pinned release predates the "
                    "announced-source contract the leg stages a forgery "
                    f"of: {unkeyed_own}"
                )
            foreign.stage(unkeyed_forged)
            foreign.announce(unkeyed_url, failures)
            pulls_at_entry = len(foreign.served_pulls())
            floor = len(pair.journal_records(unkeyed_journal))
            status, body = pair.request(f"{unkeyed_peer_url}/promote", {})
            if status not in PREEMPT_ANSWERS:
                raise Inconclusive(
                    "POST /promote on the unkeyed peer answered "
                    f"{status} {body} — the harness admits no "
                    "claim-preempt staging on the unkeyed seat either, "
                    "so the pinned release predates the verb the "
                    "involuntary demotion rides on"
                )
            if status != 200:
                failures.append(
                    "POST /promote on the unkeyed peer answered "
                    f"{status} {body} — the unkeyed pair's field claim "
                    "could not be preempted"
                )
                raise Abort
            settled = drive(
                [unkeyed_url, unkeyed_peer_url],
                DEMOTE_ROUNDS,
                lambda roles: role_of(roles[1]) == "active"
                and role_of(roles[0]) == "standby"
                and tracking_source_fallback.sync_kind(roles[0])
                in CONVERGED_VERDICTS,
                failures,
                "the unkeyed pair's involuntary demotion",
            )
            record = episode_audit(
                unkeyed_url,
                unkeyed_journal,
                floor,
                foreign,
                unkeyed_peer_url,
                urls,
                False,
                failures,
                "the unkeyed consumer pair",
            )
            unkeyed_pulls = record["verify_pulls"] - pulls_at_entry
            evidence["unkeyed"] = {
                "promote": {"status": status, "body": body},
                "rounds": (settled or {}).get("rounds"),
                "audit": record,
                "foreign_pulls": unkeyed_pulls,
            }
            if unkeyed_pulls >= UNKEYED_PULL_BOUND:
                failures.append(
                    f"the unkeyed owner spent {unkeyed_pulls} pulls at "
                    "the foreign endpoint across its involuntary "
                    "demotion — a bare hint on an unkeyed run is no "
                    "tracking source at all, so it earns not even a "
                    "bounded verify pass"
                )
            digest_entries.append(
                {
                    "phase": "unkeyed",
                    "converge_rounds": unkeyed_settled["rounds"],
                    "rounds": (settled or {}).get("rounds"),
                    "foreign_pulls": unkeyed_pulls,
                    "audit": {
                        "role": record["role"],
                        "sync": record["sync"],
                        "source_owns_field": record["source_owns_field"],
                        "line_owner": tracking_source_fallback.peer_kind(
                            urls, record["line_owner"]
                        ),
                        "adopted": record["adopted"],
                        "walk": record["walk"],
                        "losses": record["losses"],
                    },
                }
            )
        finally:
            pair.stop(unkeyed_peer)
            pair.stop(unkeyed)
            pair.stop(unkeyed_plant)
            shutil.rmtree(scratch, ignore_errors=True)

        # The restore — the manifest's declared launch roles standing
        # again for the next leg: the declared duty controller owns the
        # field with the declared standby tracking it.
        restore_roles(
            duty_url,
            standby_url,
            failures,
            DEMOTE_ROUNDS,
            "the launch-role restore",
        )
        final = rig.converge(failures, count=CONVERGE_SCANS)
        standing = [
            name
            for name, report in (
                (duty_decl["name"], final["duty_role"]),
                (standby_decl["name"], final["standby_role"]),
            )
            if role_of(report) == "active"
        ]
        if standing != [duty_decl["name"]] or not claim_reclaim.tracking(
            final["standby_role"]
        ):
            failures.append(
                "the pair never restored its launch roles — the duty "
                f"controller answers {final['duty_role']} and the "
                f"declared standby {final['standby_role']}"
            )
        evidence["restored"] = final["ticks"][-1]
        digest_entries.append(
            {
                "phase": "restore",
                "ticks": final["ticks"],
                "duty_role": final["duty_role"],
                "standby_role": final["standby_role"],
                "actives": [
                    duty_decl["name"] if name == duty_decl["name"] else "other"
                    for name in standing
                ],
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if foreign is not None:
            try:
                foreign.stop()
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
        choices=["expect-adopted"],
        help="doctor the leg's expectation to the pre-contract shape — "
        "asserting the involuntarily demoted peer adopted the foreign "
        "endpoint's announced hint — so the pass must fail naming the "
        "record it observed",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = involuntary_demote_verify_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "involuntary-demote-verify: an inconclusive run under "
                f"the {args.tamper} doctor offers the doctored case no "
                "evidence"
            )
            return 1
        eprint(f"involuntary-demote-verify: inconclusive — {inconclusive}")
        print(f"involuntary-demote-verify-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"involuntary-demote-verify: {line}")
        return 1
    for failure in failures:
        eprint(f"involuntary-demote-verify: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"involuntary-demote-verify: the {args.tamper} case "
                "passed silently — the leg never noticed the demoted "
                "peer adopting the foreign endpoint's hint"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"involuntary-demote-verify-digest {digest} — the pair "
        f"converged by tick {evidence['converged']}, the foreign "
        f"endpoint announced at tick {evidence['announced_at']} and the "
        f"keyed pair's involuntary demotion settled in "
        f"{evidence['keyed']['rounds']} rounds with "
        f"{evidence['keyed']['audit']['verify_pulls']} verify pull, the "
        f"unkeyed pair's in {evidence['unkeyed']['rounds']} rounds with "
        f"{evidence['unkeyed']['foreign_pulls']}, and the launch roles "
        f"restored at tick {evidence['restored']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
