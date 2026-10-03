#!/usr/bin/env python3
"""The usurped-claim-reclaim leg for the reference plant — the
consumer-boundary mirror of the rig's usurped-verdict reclaim
scenario (WW-ENG-003, WW-LCM-001 — the #1410 contract, pinned on the
deployment shape it exists for: this reference deployment's pair is
**keyed**, `deploy/compose.yaml` carrying the same `--pair-token` on
both controllers, exactly the shape the keyed diagnosis covers).

`--pair-token` authenticates the pair's checkpoint pulls and its
announced-source adoption. It never authenticated the field's own
claim arbitration: `claim_writer` stayed unauthenticated and
unconditional on both claim domains, so a process outside the
deployment — no key, no manifest entry, no compose service — converged
on the keyed pair entirely through public, unkey-gated pulls and ran
the unconditional claim on `POST /promote`, taking the field outright.
The keyed owner fencing-demoted, both keyed peers fell back on a
line that owns nothing, and every recovery on the pair's own surface
then met the conditional orphan grant's live-incumbent refusal:
`POST /promote` answered `409 field_claim_failed` on both peers for as
long as the foreign writer lived, so the only remedies were operator
actions on the *usurper* — demote it, or kill it.

The unkeyed corner of this same field already has its consumer leg: the
foreign-claim-release leg stages a monitor-less claim and its two
resolutions on a pair launched *without* the token, the shape its own
finding reproduced on. This leg is the keyed counterpart — the
deployment's shape, the one the diagnosis was written for — so the
narrowing, the reclaim, and the named pair fault are pinned here, on
the pair a customer actually deploys.

The contract closes it in three moves, and this leg exercises all three
on the deployed pair. The keyed pair *narrows* its ownerless verdict:
the field's own arbitration names the standing writer's monitor
endpoint, the keyed surface pulls that endpoint under a fresh `?prove=`
nonce, and only an answer that carries no valid line proof counts — so
the report becomes `usurped` ("the tracked line has no field owner and
the writer the field does name is not this line's") beside
`field_claim: held`, the inverse pairing `orphaned` alone cannot
express. The pair's own promotion gate routes that narrower verdict to
the **unconditional** claim instead of the conditional orphan grant, so
`POST /promote` takes the field back from the live writer — and
journals `foreign_claim_preempted` naming the endpoint it took it from,
the audit counterpart of the `field_claim_lost` that began the episode.
The pair view reports the condition by name, so a consumer reads "the
field has a writer that is not this pair's" as its own verdict rather
than as ownerlessness: `dcs-ctl <addr> pair-health <peer>` names
`standby_usurped` and exits nonzero while it stands.

With the manifest-declared pair launched keyed through the shared
launch harness and settled — the duty controller `active`, its declared
standby `tracking` — the driven run:

- reads the deployment's own declaration that this pair is the keyed one
  the contract is written for: every manifest-declared controller's
  compose invocation carrying the same `--pair-token`, the pair's shared
  tracking secret the diagnosis asks its evidence under. A definition
  that declares the pair without it — or with two different secrets — is
  a pair the keyed diagnosis cannot serve, and the leg names that rather
  than quietly reporting an inconclusive run;
- gates the contract surface the leg's verdict reads: the launched
  owner's recorded claim token, the field's own arbitration holding
  write-ownership under it and naming its dialable monitor, the served
  checkpoint's ownership stamps, and the pair's own monitors stamping a
  keyed `line_proof` under a `?prove=` nonce while an unkeyed process on
  the same plant cannot;
- stages the seizure with a **raw plant-socket attachment**: a bare
  `dcs-controller --remote <plant> --driven --listen` process carrying
  the pair's own standby wiring and *no* `--pair-token` — the shape a
  process outside the deployment presents, converging on the keyed pair
  through public pulls alone. It takes nothing while it converges (the
  control: the pair holds the field under its own token, untouched),
  then `POST /promote` on it runs the unconditional claim and the field
  moves to it;
- asserts the seizure end to end: the field's own arbitration now names
  the unkeyed writer's token and *its* dialable monitor, the superseded
  keyed owner fencing-demotes in place with one journaled
  `field_claim_lost` attributed to that token;
- asserts the narrower verdict on **both** keyed peers beside
  `field_claim: held`, and the evidence it rests on read directly off
  the wire: the seized writer's declared monitor answers the `?prove=`
  nonce pull with no line proof while the pair's own monitors answer
  one, and each keyed peer journals its own refusal of that document
  naming the endpoint it came from. The pair-health read through the
  shipped `dcs-ctl` names `standby_usurped` for both members and exits
  nonzero;
- asserts the self-service recovery: `POST /promote` on the demoted
  keyed owner — answered under the `usurped` verdict it read — takes
  the field back from the live writer, the field's arbitration names
  the keyed owner again, the usurper is fenced out of the field like
  any superseded writer, and the journal names the endpoint the claim
  was taken from on both the served journal and the manifest-declared
  durable file. Across the reclaim window no keyed peer may read the
  recovered writer as usurping the field — the narrowing is a reading
  about that writer, not a latch on the pair;
- ends with the pair reconverged: driven tracking-first ticks proving
  identical images, the launch roles standing (the manifest's duty
  controller `active`, its declared standby `tracking`), the
  pair-health verdict clean again, and the episode's records in the
  manifest-declared durable journal.

The contract postdates some released lines. Where the launched tooling
predates it — no owner-token claim line, a fencing verdict naming no
owner or no declared monitor, a declared bind stored verbatim instead
of the claim-monitor normalization, a served checkpoint without the
ownership stamps, a monitor answering no keyed `line_proof`, a role
report carrying no sync vocabulary or no `field_claim` observation, the
seizure's unkeyed attachment never converging on the keyed pair, the
keyed peers settling the *plain* `orphaned` verdict where the narrower
one belongs, a reclaim promotion refused `field_claim_failed`, an
unjournaled refused foreign-writer document or preempted endpoint, or a
pair-health read carrying no named fault vocabulary — the run classifies **inconclusive**, never a product
failure, and reports it rather than asserting until the manifest repins
a release carrying the contract.

Usage:

    usurped_claim_reclaim.py --plant-server PATH --controller PATH \
        --ctl PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `usurped-claim-reclaim-digest <sha256>` line prints —
the check runs two passes and compares them
(`usurped-claim-reclaim-nondeterministic`). A contract violation
reports `usurped-claim-reclaim: …` lines on stderr and exits 1 — the
check's `usurped-claim-reclaim-failed`. `--tamper expect-refused-claim`
doctors the reclaim's own expectation to the recorded defect: the
promotion refused with the conditional orphan grant's live-incumbent
`field_claim_failed` rather than reclaiming the field from the live
writer, so the leg proves its reclaim assertion fires on the honest
grant instead of passing an unexercised contract.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
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
# digest-identical passes, and exercise its doctored cases.
# The doctored case: the reclaim's promotion refused with the
# conditional orphan grant's live-incumbent `field_claim_failed` — the
# recorded defect, where nothing on the pair's own surface could take
# the field back from a live writer — must surface the named
# diagnostic rather than passing an unexercised contract.
LEG = {
    "order": 800,
    "title": "the usurped-claim-reclaim leg",
    "passes": "usurped-claim-reclaim",
    "tools": {
        "ctl": "dcs-ctl",
    },
    "tampers": [
        {
            "name": "expect-refused-claim",
            "passed": "an expect-refused-claim case passed the usurped-claim-reclaim leg",
            "missed": "the expect-refused-claim case did not report its named diagnostic",
            "evidence": ["refused with a field_claim_failed refusal"],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


def service_argvs(path):
    """The per-service invocations `deploy/compose.yaml` declares — a
    stdlib read of the checked-in rig definition, `{service: [arg, …]}`
    over each service's `command:` list. Only the invocations are read:
    the `deploy` stage owns the definition's full consistency check, and
    this leg reads one field of it — the pair's shared `--pair-token` —
    so a drift in the definition's shape never has to be interpreted
    here beyond that."""
    services = {}
    name = None
    in_command = False
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            header = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line.rstrip())
            if header:
                name = header.group(1)
                services.setdefault(name, [])
                in_command = False
                continue
            if name is None:
                continue
            if in_command:
                item = re.match(r"^\s*-\s*(.*)$", line.rstrip())
                if item:
                    services[name].append(
                        item.group(1).strip().strip('"').strip("'")
                    )
                    continue
                in_command = False
            if re.match(r"^    command:\s*$", line.rstrip()):
                in_command = True
    return services


def declared_keying(args, declared):
    """The deployment's own declaration that this pair is keyed: every
    manifest-declared controller's compose invocation carrying the same
    `--pair-token` — the pair's shared tracking secret the keyed
    diagnosis asks its evidence under. A pair declared without it is a
    pair the diagnosis cannot serve, so the shape this leg pins is named
    rather than silently reported as an inconclusive run.
    Returns the declared `{controller: token}` map."""
    path = os.path.join(os.path.dirname(args.manifest), "compose.yaml")
    if not os.path.exists(path):
        raise Abort(
            f"the deployment declares no rig definition beside "
            f"{args.manifest} — the checked-in compose file is what "
            "carries this pair's shared tracking secret"
        )
    _manifest, duty_decl, standby_decl = declared
    services = service_argvs(path)
    tokens = {}
    for entry in (duty_decl, standby_decl):
        argv = services.get(entry["name"])
        if argv is None:
            raise Abort(
                f"the rig definition declares no {entry['name']} service "
                f"to read a --pair-token from: {sorted(services)}"
            )
        if "--pair-token" not in argv:
            raise Abort(
                f"the rig definition's {entry['name']} invocation "
                f"carries no --pair-token — the deployed pair is not the "
                "keyed pair this leg's contract is written for: "
                f"{argv}"
            )
        index = argv.index("--pair-token") + 1
        if index >= len(argv):
            raise Abort(
                f"the rig definition's {entry['name']} invocation ends "
                f"at a bare --pair-token — the pair's shared tracking "
                "secret is undeclared: "
                f"{argv}"
            )
        tokens[entry["name"]] = argv[index]
    if len(set(tokens.values())) != 1:
        raise Abort(
            f"the rig definition's controllers declare different "
            f"--pair-token values {tokens} — a keyed pair is one line "
            "sharing one secret, and the diagnosis asks its evidence "
            "under that key"
        )
    return tokens


class Inconclusive(Exception):
    """The pinned release predates — or never covers — the contract
    the leg exercises: the run classifies inconclusive, never a product
    failure. The first arg is the stable reason the `inconclusive`
    digest line prints — two identical passes must share it — and the
    optional second arg the run's own evidence, reported on stderr
    only, where ephemeral verdicts, endpoints, and minted tokens
    belong."""


# The driven-scan bounds the episode's phases run. The unkeyed
# attachment converges on the keyed pair through ordinary pulls (a
# couple of scans), the fenced demote-in-place settles inside one, the
# narrower verdict is reached on the next pair of tracking pulls, and
# the reclaim settles on its own first scan — each window left with
# room to spare, because the defect this leg convicts is the indefinite
# `409 field_claim_failed` strand the bounded reclaim closes, not a
# race the bound has to catch.
ATTACHMENT_SCANS = 4
SEIZE_ROUNDS = 6
RECLAIM_ROUNDS = 6

# The nonce the leg's own evidence pulls carry — a keyed `?prove=` pull
# names the nonce its answer must attest to, and a fresh one per run is
# what makes an answer a proof rather than a replay. Fixed here so the
# leg's own reads stay identical across its two passes; the pair's
# diagnosis mints its own.
PROVE_NONCE = 0x0DC5


def monitor_addr(url):
    """The peer's dialable monitor address as `host:port` — the form the
    normalized claim declaration names once the wildcard bind resolves
    to the claim connection's proven source."""
    return url.removeprefix("http://")


def monitor_kind(declared, owner_addr):
    """Classify a claim-declared monitor: `wildcard` for an
    unspecified bind address — the verbatim declaration the
    claim-monitor normalization substitutes — `owner` for the claiming
    peer's own dialable monitor, `foreign` for anything else. Only
    `owner` is a contract answer."""
    text = str(declared)
    host = text.rsplit(":", 1)[0]
    if host in ("0.0.0.0", "::", "[::]"):
        return "wildcard"
    if text == owner_addr:
        return "owner"
    return "foreign"


def prove_pull(url, failures, what):
    """`GET /checkpoint?prove=<nonce>` — the keyed evidence pull the
    pair's diagnosis runs against a standing writer's declared monitor.
    Returns the served document; `line_proof` is absent on any answer
    that is not one this line's key can produce."""
    return pair.get(
        f"{url}/checkpoint?prove={PROVE_NONCE}", what, failures
    )


def ctl_pair_health(ctl, addr, peer):
    """One `dcs-ctl <addr> pair-health <peer>` through the shipped
    operator CLI — the consumer's read of the pair view's named
    redundancy faults. Returns `(exit, verdict_or_None, stderr)`: the
    printed verdict is decoded whenever the CLI printed one, faults or
    not (the CLI exits nonzero while any fault stands)."""
    argv = [ctl, addr, "pair-health", peer]
    try:
        run = subprocess.run(argv, capture_output=True, text=True,
                             timeout=30)
    except Exception as error:
        return None, None, f"dcs-ctl pair-health raised {error!r}"
    verdict = None
    try:
        verdict = json.loads(run.stdout)
    except (json.JSONDecodeError, ValueError):
        verdict = None
    if not isinstance(verdict, dict):
        verdict = None
    return run.returncode, verdict, run.stderr.strip()


def scan_attachment(url, failures):
    """One driven scan through the raw attachment's `POST /scan` —
    the same driven surface every peer on this deployment answers."""
    return pair.scan(url, failures)


def scan_trio(standby_url, duty_url, foreign_url, failures):
    """One driven round of the whole episode — the tracking peer first
    (its pull applying the owner's latest checkpoint), then the field
    owner, then the foreign writer, so every peer's served verdict is
    read against a stream none of them is behind."""
    for url in (standby_url, duty_url, foreign_url):
        pair.scan(url, failures)


def verdict_row(report):
    """A peer's served redundancy verdict as the digest and failure
    lines read it: `<role>/<sync>` beside the field's own arbitration
    word."""
    sync = stranded_rejoin.sync_kind(report)
    return f"{report.get('role')}/{sync}"


def claim_probed(probe_io):
    """One non-holder's field-mutation probe through a dedicated
    plant-socket attachment — the field's own arbitration verdict for
    whoever stands in the claim, read straight off the protocol's
    fencing refusal."""
    return probe_io.request({"op": "step", "dt": 0})


def field_claim_word(report):
    """The field's own arbitration observation a RoleReport carries —
    `held`, `unclaimed` — or None where the report carries none."""
    return report.get("field_claim")


def gating(rig, probe_io, failures, evidence):
    """The contract surface the leg's verdict reads, gated on the
    converged keyed pair: the launched owner's recorded claim token,
    the field's arbitration holding write-ownership under it and naming
    its dialable monitor, the served checkpoint's ownership stamps, and
    both pair members stamping a keyed `line_proof` under a `?prove=`
    nonce. Each absence is the release predating the substrate, never a
    violation. Returns the owner's claim token."""
    duty_url = rig.duty_url
    owner_token = failover.owner_token(rig.duty_preamble)
    if owner_token is None:
        raise Inconclusive(
            "the launched field owner recorded no owner-token claim "
            "line — the pinned release claims only on promotion, "
            "predating the claim lifecycle this leg's arbitration "
            "reads",
            f"the duty startup preamble was {rig.duty_preamble}",
        )
    probe = claim_probed(probe_io)
    evidence["gate_probe"] = probe
    if not claim_reclaim.mutation_fenced(probe):
        raise Abort(
            "the field held no writer claim after convergence — a "
            f"third-party probe answered {probe}, so the leg has no "
            "standing claim for a foreign writer to take"
        )
    if claim_reclaim.verdict_owner(probe) is None:
        raise Inconclusive(
            "the fencing verdict names no standing owner — the pinned "
            "release predates the verdict attribution this leg's "
            "readers name",
            f"the standing fencing verdict was {probe}",
        )
    if claim_reclaim.verdict_owner(probe) != owner_token:
        raise Abort(
            "the standing claim names token "
            f"{claim_reclaim.verdict_owner(probe)}, not the launch "
            f"owner's recorded token {owner_token} — the pair is not "
            "in its launch claim state"
        )
    declared = stranded_rejoin.verdict_monitor(probe)
    if declared is None:
        raise Inconclusive(
            "the standing claim declares no monitor — the pinned "
            "release predates the claim-declared monitor the usurped "
            "diagnosis pulls",
            f"the standing fencing verdict was {probe}",
        )
    kind = monitor_kind(declared, monitor_addr(duty_url))
    if kind == "wildcard":
        raise Inconclusive(
            "the standing claim declares its wildcard bind verbatim — "
            "the pinned release predates the claim-monitor "
            "normalization the diagnosis reads",
            f"the declared monitor was {declared}",
        )
    if kind != "owner":
        raise Abort(
            f"the standing claim declares monitor {declared} — not "
            "the field owner's dialable monitor "
            f"{monitor_addr(duty_url)}"
        )
    report = pair.get(f"{duty_url}/role", "GET /role", failures)
    if field_claim_word(report) is None:
        raise Inconclusive(
            "the served role report carries no field-claim "
            "observation — the pinned release predates the "
            "arbitration half the narrower verdict is reported "
            "beside",
            f"GET /role answered {report}",
        )
    if field_claim_word(report) != "held":
        raise Abort(
            f"the field owner reports claim {field_claim_word(report)!r} "
            "after convergence — the launch pair does not hold the "
            "field to lose"
        )
    doc = pair.get(
        f"{duty_url}/checkpoint", "GET /checkpoint", failures
    )
    if "source_owns_field" not in doc or "line_owner" not in doc:
        raise Inconclusive(
            "the served checkpoint carries no field-ownership stamps — "
            "the pinned release predates the field-arbitrated monitor "
            "contract the diagnosis reads",
            f"the checkpoint serves {sorted(doc)}",
        )
    proofs = {}
    for name, url in (
        (rig.duty_decl["name"], duty_url),
        (rig.standby_decl["name"], rig.standby_url),
    ):
        pulled = prove_pull(url, failures, f"GET /checkpoint?prove= on {name}")
        proofs[name] = "line_proof" in pulled
        if not proofs[name]:
            raise Inconclusive(
                "a keyed pair member answered a ?prove= nonce pull "
                "with no line proof — the pinned release predates the "
                "keyed line proof this leg's diagnosis asks under",
                f"{name} served {sorted(pulled)}",
            )
    evidence["gate_proofs"] = proofs
    return owner_token


def seizure(rig, args, probe_io, owner_token, floors, failures, evidence):
    """The seizure itself: the raw plant-socket attachment — a bare
    `dcs-controller --remote` process on the deployment's own plant,
    the pair's standby wiring, no `--pair-token` — converging on the
    keyed pair through public pulls, taking nothing while it
    converges, and then taking the field outright on `POST /promote`.
    Returns the attachment's `(process, url, monitor)` — the endpoint
    its claim declared, which the reclaim's audit must later name."""
    duty_url, standby_url = rig.duty_url, rig.standby_url
    target = monitor_addr(duty_url)
    foreign, foreign_url, preamble = pair.spawn_peer(
        args.controller,
        args.model,
        args.dt,
        rig.plant_addr,
        target,
        {},
    )
    if foreign_url is None:
        pair.stop(foreign)
        raise Abort(
            "the unkeyed attachment exited at startup: "
            f"{'; '.join(preamble) or 'no diagnostic'}"
        )
    evidence["attachment_preamble"] = preamble
    try:
        # The attachment converges on the keyed pair through public,
        # unkey-gated pulls alone — the finding's own convergence.
        for _ in range(ATTACHMENT_SCANS):
            scan_attachment(foreign_url, failures)
        settled = pair.get(
            f"{foreign_url}/role", "GET /role", failures
        )
        if not claim_reclaim.tracking(settled):
            raise Abort(
                "the unkeyed attachment never converged tracking on the "
                "keyed owner — the leg's seizure is staged on an "
                f"attachment the pair refuses: {settled}"
            )

        # The control: converging on the pair took nothing. The field
        # still stands under the launch owner's own token, the sibling
        # still tracks it, and a third-party probe still meets the
        # launch claim.
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        sibling_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        control = claim_probed(probe_io)
        if duty_role.get("role") != "active" or not (
            claim_reclaim.tracking(sibling_role)
        ):
            failures.append(
                "the keyed pair moved while the unkeyed attachment "
                f"merely converged — {duty_role} / {sibling_role}"
            )
            raise Abort
        if claim_reclaim.verdict_owner(control) != owner_token:
            failures.append(
                "the unkeyed attachment's convergence took the field — "
                "the standing claim names "
                f"{claim_reclaim.verdict_owner(control)}, not the launch "
                f"owner's recorded token {owner_token}: {control}"
            )
            raise Abort
        evidence["control"] = "pair-untouched"

        # The preempt: the unkeyed attachment's `POST /promote` runs
        # the unconditional claim. Nothing authenticates it — the
        # field's arbitration has no key to ask under, which is the
        # whole of the finding.
        status, report = pair.request(f"{foreign_url}/promote", {})
        evidence["attachment_promote"] = (status, report)
        if status != 200 or report.get("role") != "promoting":
            if status == 409:
                raise Inconclusive(
                    "the unkeyed attachment's promote was refused — the "
                    "pinned release predates the unconditional "
                    "promotion claim this leg seizes the field with",
                    f"POST /promote answered {status} {report}",
                )
            failures.append(
                f"POST /promote on the unkeyed attachment answered "
                f"{status} {report}, expected a promoting report"
            )
            raise Abort

        # The seize settles: the field moves to the unkeyed writer and
        # the superseded keyed owner fencing-demotes in place.
        rows = []
        demoted = None
        for _ in range(SEIZE_ROUNDS):
            scan_trio(standby_url, duty_url, foreign_url, failures)
            duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
            rows.append(verdict_row(duty_role))
            if duty_role.get("role") == "standby":
                demoted = duty_role
                break
        evidence["seize_watch"] = rows
        if demoted is None:
            failures.append(
                "the unkeyed attachment's promote did not fence the "
                "keyed owner out — the field owner never demoted, its "
                f"reports stayed {rows}"
            )
            raise Abort

        # The field's own arbitration now names the foreign writer: the
        # claim moved to the unkeyed attachment's own token, declaring
        # its own dialable monitor — the endpoint the keyed pair's
        # diagnosis will pull, and the one `foreign_claim_preempted`
        # must later name.
        seized = claim_probed(probe_io)
        evidence["seized_probe"] = seized
        if not claim_reclaim.mutation_fenced(seized):
            failures.append(
                "the unkeyed attachment's promote did not take the "
                f"field — a third-party probe answered {seized}"
            )
            raise Abort
        foreign_owner = claim_reclaim.verdict_owner(seized)
        if foreign_owner is None:
            raise Inconclusive(
                "the post-seizure fencing verdict names no standing "
                "owner — the pinned release predates the verdict "
                "attribution",
                f"the fencing verdict was {seized}",
            )
        if foreign_owner == owner_token:
            failures.append(
                "the field's claim still names the launch owner's token "
                f"{owner_token} after the unkeyed attachment promoted — "
                f"the seizure did not take the field: {seized}"
            )
            raise Abort
        foreign_monitor = stranded_rejoin.verdict_monitor(seized)
        if foreign_monitor is None:
            raise Inconclusive(
                "the seizing claim declares no monitor — the pinned "
                "release predates the claim-declared monitor the "
                "diagnosis probes and the journal names",
                f"the fencing verdict was {seized}",
            )
        kind = monitor_kind(foreign_monitor, monitor_addr(foreign_url))
        if kind == "wildcard":
            raise Inconclusive(
                "the seizing claim declares its wildcard bind verbatim — "
                "the pinned release predates the claim-monitor "
                "normalization",
                f"the declared monitor was {foreign_monitor}",
            )
        if kind != "owner":
            failures.append(
                f"the seizing claim declares monitor {foreign_monitor} "
                "— not the unkeyed writer's own dialable monitor "
                f"{monitor_addr(foreign_url)}"
            )
            raise Abort

        # The audited loss on the superseded keyed owner: one
        # `field_claim_lost` attributed to the foreign token, beside
        # the fenced demote walk.
        added = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )[floors["duty"]:]
        losses = stranded_rejoin.lost_entries(added)
        evidence["seize_losses"] = losses
        if not losses:
            raise Inconclusive(
                "the fenced demotion left no field_claim_lost on the "
                "superseded owner's journal — the pinned release "
                "predates the loss record this episode's audit reads",
                f"the added journal carried {added}",
            )
        if len(losses) != 1:
            failures.append(
                f"expected exactly one field_claim_lost above the "
                f"journal floor, found {len(losses)} — one record per "
                "held claim, not one per fenced write"
            )
            raise Abort
        if losses[0][1].get("claimant") != foreign_owner:
            failures.append(
                "the journaled fencing loss attributes the takeover "
                f"to {losses[0][1].get('claimant')}, not the unkeyed "
                f"writer's token {foreign_owner}"
            )
            raise Abort
        walk = stranded_rejoin.role_walk(added)
        if walk != [
            ("active", "demoting", "fenced"),
            ("demoting", "standby", "fenced"),
        ]:
            failures.append(
                "the superseded owner's journaled role walk is "
                f"{walk}, expected the fenced active → demoting → "
                "standby"
            )
            raise Abort
        evidence["seized_at"] = demoted.get("tick")
        evidence["foreign_owner"] = foreign_owner
        return foreign, foreign_url, foreign_monitor
    except Exception:
        pair.stop(foreign)
        raise


def narrower_verdict(
    rig, args, foreign_url, foreign_monitor, floors, failures, evidence
):
    """The narrower verdict, on both keyed peers, beside the field's own
    `held` arbitration — and the evidence it rests on, read straight off
    the wire: the seized writer's declared monitor answers the `?prove=`
    nonce pull with no line proof while the pair's own monitors answer
    one, and each keyed peer journals its own refusal of that document
    naming the endpoint. The pair-health read through the shipped
    `dcs-ctl` names the condition `standby_usurped` for both members."""
    duty_url, standby_url = rig.duty_url, rig.standby_url
    rows = []
    seen = {rig.duty_decl["name"]: set(), rig.standby_decl["name"]: set()}
    narrowed = None
    for _ in range(SEIZE_ROUNDS):
        scan_trio(standby_url, duty_url, foreign_url, failures)
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        sibling_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        rows.append(
            [
                verdict_row(duty_role),
                verdict_row(sibling_role),
            ]
        )
        seen[rig.duty_decl["name"]].add(
            claim_reclaim.sync_state(duty_role)
        )
        seen[rig.standby_decl["name"]].add(
            claim_reclaim.sync_state(sibling_role)
        )
        if claim_reclaim.sync_state(duty_role) == "usurped" and (
            claim_reclaim.sync_state(sibling_role) == "usurped"
        ):
            narrowed = (duty_role, sibling_role)
            break
    evidence["verdict_watch"] = rows
    if narrowed is None:
        read = {
            name: sorted(str(state) for state in states)
            for name, states in seen.items()
        }
        evidence["verdict_read"] = read
        if None in seen[rig.duty_decl["name"]] or (
            None in seen[rig.standby_decl["name"]]
        ):
            raise Inconclusive(
                "a keyed peer's role report carried no sync vocabulary "
                "while it tracked a foreign writer — the pinned release "
                "predates the served convergence verdicts this leg "
                "reads",
                f"the peers' sync readings were {read}",
            )
        if "usurped" not in read[
            rig.duty_decl["name"]
        ] and "usurped" not in read[rig.standby_decl["name"]]:
            raise Inconclusive(
                "the keyed pair never reported the narrower usurped "
                "verdict while the unkeyed writer held the field — the "
                "pinned release predates the narrower ownerless verdict "
                "this leg exercises",
                f"the peers' sync readings were {read}",
            )
        failures.append(
            "only one keyed peer narrowed its ownerless verdict to "
            f"usurped — {read} — each runs the diagnosis on the field's "
            "own arbitration independently, and a peer that never "
            "narrows keeps the conditional orphan grant"
        )
        raise Abort
    duty_role, sibling_role = narrowed
    for name, report in (
        (rig.duty_decl["name"], duty_role),
        (rig.standby_decl["name"], sibling_role),
    ):
        if report.get("role") != "standby":
            failures.append(
                f"{name} reports role {report.get('role')!r} under the "
                "usurped verdict — a peer tracking a foreign writer is "
                f"no field owner: {report}"
            )
            raise Abort
        if field_claim_word(report) != "held":
            failures.append(
                f"{name} reports field_claim "
                f"{field_claim_word(report)!r} beside the usurped "
                "verdict — the narrower verdict names the field's own "
                f"held arbitration, and this is what says so: {report}"
            )
            raise Abort

    # The diagnosis's own evidence, read over the wire: the standing
    # writer's declared monitor answers, and its answer carries no line
    # proof. The pair's own monitors answer the same nonce with one —
    # so the verdict is a reading about that writer, not about this
    # deployment being unkeyed.
    foreign_doc = prove_pull(
        foreign_url, failures,
        "GET /checkpoint?prove= on the unkeyed writer",
    )
    if "line_proof" in foreign_doc:
        failures.append(
            "the unkeyed writer's declared monitor answered the ?prove= "
            "nonce pull with a line proof — it is inside the pair's "
            "line, so the pair has no foreign writer to reclaim from: "
            f"{sorted(foreign_doc)}"
        )
        raise Abort
    duty_doc = prove_pull(
        duty_url, failures, "GET /checkpoint?prove= on the keyed owner"
    )
    if "line_proof" not in duty_doc:
        raise Inconclusive(
            "the keyed owner answered a ?prove= nonce pull with no "
            "line proof while it tracked a foreign writer — the pinned "
            "release predates the keyed line proof this diagnosis "
            "rests on",
            f"the owner served {sorted(duty_doc)}",
        )

    # The refusal is durable audit on each keyed peer that ran the
    # diagnosis: the refused document is named with the endpoint it came
    # from, so the strand the narrower verdict reports is reconstructable
    # after the fact rather than only in the served report.
    refusals = {}
    for name, url, floor in (
        (rig.duty_decl["name"], duty_url, floors["duty"]),
        (rig.standby_decl["name"], standby_url, floors["standby"]),
    ):
        added = pair.get(f"{url}/journal", "GET /journal", failures)[floor:]
        refused = [
            entry["event"]["tracking_source_refused"]
            for entry in added
            if "tracking_source_refused" in entry.get("event", {})
        ]
        refusals[name] = [
            record
            for record in refused
            if record.get("source") == foreign_monitor
            and "pair key" in str(record.get("detail"))
        ]
        evidence[name + "_refusals"] = refusals[name]
        if not refusals[name]:
            raise Inconclusive(
                f"{name} journaled no refused foreign-writer document "
                f"naming {foreign_monitor} — the pinned release "
                "predates the durable refusal the narrower verdict "
                "reports on",
                f"{name} added {refused}",
            )

    # The pair view's named vocabulary, read through the shipped
    # operator CLI: the condition is a redundancy fault of its own kind
    # and the CLI exits nonzero while it stands.
    code, verdict, detail = ctl_pair_health(
        args.ctl, monitor_addr(duty_url), monitor_addr(standby_url)
    )
    evidence["pair_health"] = (code, verdict)
    if verdict is None:
        raise Inconclusive(
            "the pair-health read carried no verdict document — the "
            "released tooling predates the named redundancy-fault "
            "vocabulary this leg reads",
            f"dcs-ctl pair-health exited {code}: {detail}",
        )
    fault_kinds = verdict.get("fault_kinds")
    version = verdict.get("fault_kinds_version")
    if not isinstance(fault_kinds, list) or version is None:
        raise Inconclusive(
            "the pair-health verdict carries no named fault vocabulary "
            "— the released tooling predates decision 19's versioned "
            "pair-fault kinds",
            f"the verdict was {verdict}",
        )
    named = fault_kinds.count("standby_usurped")
    if named == 0:
        raise Inconclusive(
            "the pair-health verdict names no standby_usurped fault "
            "while both keyed peers read the field as held by a writer "
            "outside their line — the pinned release predates the "
            "named redundancy fault",
            f"the pair-health verdict was {verdict}",
        )
    if named != 2:
        failures.append(
            f"the pair-health verdict names standby_usurped for "
            f"{named} peer(s), expected both keyed members — each "
            "reads the field's arbitration and diagnoses it "
            f"independently: {verdict}"
        )
        raise Abort
    if code == 0:
        failures.append(
            "the pair-health read exited zero while the named "
            "standby_usurped fault stood — the CLI's exit status must "
            f"report the redundancy fault: {verdict}"
        )
        raise Abort
    return named, version


def reclaim(
    rig, probe_io, foreign_url, foreign_monitor, owner_token, floors,
    failures, evidence, tamper,
):
    """The self-service recovery on the pair's own surface: `POST
    /promote` on the demoted keyed owner routes the narrower verdict to
    the unconditional claim, takes the field back from the live foreign
    writer, journals the endpoint it took it from, and fences the
    usurper out — with no keyed peer reading the recovered writer as
    usurping the field."""
    duty_url, standby_url = rig.duty_url, rig.standby_url
    status, report = pair.request(f"{duty_url}/promote", {})
    evidence["reclaim_promote"] = (status, report)
    answered = report if isinstance(report, dict) else {}
    if tamper == "expect-refused-claim":
        # The doctored expectation: the recorded defect's answer, where
        # the narrower verdict is unknown and the conditional orphan
        # grant refuses the live incumbent.
        if status != 409 or "field_claim_failed" not in answered:
            failures.append(
                f"POST /promote on the demoted keyed owner answered "
                f"{status} {answered} — the doctored case wants the "
                "reclaim's promotion refused with a field_claim_failed "
                "refusal"
            )
            raise Abort
        return
    if status == 409:
        if "field_claim_failed" in answered:
            raise Inconclusive(
                "the demoted keyed owner's promote was refused with "
                "field_claim_failed — the pinned release predates the "
                "reclaim this leg's narrower verdict arms, leaving the "
                "pair operator actions on the usurper alone",
                f"POST /promote answered {status} {answered}",
            )
        failures.append(
            f"POST /promote on the demoted keyed owner answered {status} "
            f"{answered}, expected a promoting report"
        )
        raise Abort
    if status != 200 or answered.get("role") != "promoting":
        failures.append(
            f"POST /promote on the demoted keyed owner answered {status} "
            f"{answered}, expected a promoting report"
        )
        raise Abort
    if claim_reclaim.sync_state(answered) != "usurped":
        failures.append(
            "the reclaim's promotion was answered under the sync "
            f"verdict {claim_reclaim.sync_state(answered)!r}, not "
            "usurped — the promotion gate did not read the narrower "
            f"verdict: {answered}"
        )
        raise Abort
    evidence["reclaim_gate"] = "usurped"
    evidence["reclaimed_at"] = answered.get("tick")

    rows = []
    recovered = None
    fenced = None
    stray = []
    for _ in range(RECLAIM_ROUNDS):
        scan_trio(standby_url, duty_url, foreign_url, failures)
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        foreign_role = pair.get(
            f"{foreign_url}/role", "GET /role", failures
        )
        sibling_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        rows.append(
            [
                verdict_row(duty_role),
                verdict_row(foreign_role),
                verdict_row(sibling_role),
            ]
        )
        # The safety half: the narrowing is a reading about the writer
        # that stood, so a writer back inside the line is never read as
        # usurping the field.
        for name, report_ in (
            (rig.duty_decl["name"], duty_role),
            (rig.standby_decl["name"], sibling_role),
        ):
            if claim_reclaim.sync_state(report_) == "usurped":
                stray.append(f"{name}={verdict_row(report_)}")
        if recovered is None and duty_role.get("role") == "active":
            recovered = duty_role
        if fenced is None and foreign_role.get("role") != "active":
            fenced = foreign_role
        if recovered is not None and fenced is not None:
            break
    evidence["reclaim_watch"] = rows
    if stray:
        failures.append(
            "a keyed peer read the recovered owner as usurping its own "
            f"field — the narrowing is a reading about the standing "
            f"writer, never a latch on the pair: {stray}"
        )
        raise Abort
    evidence["verdict"] = "cleared"
    if recovered is None:
        failures.append(
            "the reclaim's promotion never settled the keyed owner "
            "active — it took the claim and lost the field again: "
            f"{rows}"
        )
        raise Abort
    if fenced is None:
        failures.append(
            "the unkeyed usurper stayed active across the reclaim — the "
            "field did not fence the writer it was taken from: "
            f"{rows}"
        )
        raise Abort

    # The field's own arbitration names the keyed owner again, under its
    # own token and its own declared monitor.
    probe = claim_probed(probe_io)
    evidence["reclaimed_probe"] = probe
    if not claim_reclaim.mutation_fenced(probe):
        failures.append(
            f"the reclaimed field is not fenced against third-party "
            f"mutations: {probe}"
        )
        raise Abort
    if claim_reclaim.verdict_owner(probe) != owner_token:
        failures.append(
            "the field's claim did not return to the keyed owner's "
            f"recorded token {owner_token} — the reclaim named "
            f"{claim_reclaim.verdict_owner(probe)}: {probe}"
        )
        raise Abort
    declared = stranded_rejoin.verdict_monitor(probe)
    if declared is None:
        raise Inconclusive(
            "the reclaimed claim declares no monitor — the pinned "
            "release predates the claim-declared monitor the reclaim "
            "is audited against",
            f"the fencing verdict was {probe}",
        )
    if monitor_kind(declared, monitor_addr(duty_url)) != "owner":
        failures.append(
            f"the reclaimed claim declares monitor {declared} — not "
            "the keyed owner's dialable monitor "
            f"{monitor_addr(duty_url)}"
        )
        raise Abort

    # The audit: the pair's own record names the endpoint the claim was
    # taken from — the counterpart of the loss record that began the
    # episode, on the served journal and in the manifest-declared
    # durable file.
    added = pair.get(
        f"{duty_url}/journal", "GET /journal", failures
    )[floors["duty"]:]
    preempted = [
        (entry["seq"], entry["event"]["foreign_claim_preempted"])
        for entry in added
        if "foreign_claim_preempted" in entry.get("event", {})
    ]
    evidence["preempted"] = preempted
    if not preempted:
        raise Inconclusive(
            "the reclaim journaled no foreign_claim_preempted record — "
            "the pinned release predates the audit counterpart naming "
            "the endpoint the field was taken from",
            f"the added journal carried "
            f"{[sorted(entry.get('event', {})) for entry in added]}",
        )
    if len(preempted) != 1:
        failures.append(
            f"the reclaim journaled {len(preempted)} "
            "foreign_claim_preempted records — one per granted claim "
            "taken from a diagnosed foreign writer, not one per probe"
        )
        raise Abort
    named_endpoint = preempted[0][1].get("writer")
    if named_endpoint != foreign_monitor:
        failures.append(
            "the journaled preempted claim names endpoint "
            f"{named_endpoint}, not the usurper's declared monitor "
            f"{foreign_monitor} — the audit must say which process held "
            "the field across the episode"
        )
        raise Abort
    walk = stranded_rejoin.role_walk(added)
    for step in (
        ("standby", "promoting", "request"),
        ("promoting", "active", "request"),
    ):
        if step not in walk:
            failures.append(
                "the reclaim's journaled role walk is "
                f"{walk}, missing {step} — the recovery ran no "
                "operator promotion"
            )
            raise Abort
    journal_file = rig.duty_files.get("journal_file")
    if journal_file is None:
        raise Inconclusive(
            "the manifest's pair declares no journal_file on the "
            "keyed owner — the durable half of the reclaim's audit is "
            "absent"
        )
    durable = stranded_rejoin.durable_kinds(journal_file)
    evidence["durable"] = sorted(durable)
    if "foreign_claim_preempted" not in durable:
        failures.append(
            "the manifest-declared durable journal file carries no "
            "foreign_claim_preempted record — the endpoint the claim "
            "was taken from went unrecorded across a restart"
        )
        raise Abort


def reconverge(rig, args, probe_io, owner_token, failures, evidence):
    """The pair after the reclaim: driven tracking-first ticks proving
    identical images, the launch roles standing, the field's claim back
    on the keyed owner, and the pair-health verdict clean again."""
    duty_url, standby_url = rig.duty_url, rig.standby_url
    ticks = []
    for _ in range(pair.HANDOVER_TICKS):
        _tracked, owner = rig.tick(
            standby_url,
            duty_url,
            failures,
            diverged="the reclaimed pair's image diverged at tick "
            "{tick} — the reclaim was not bumpless",
        )
        ticks.append(owner["tick"])
    duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
    sibling_role = pair.get(f"{standby_url}/role", "GET /role", failures)
    if duty_role.get("role") != "active" or not (
        claim_reclaim.tracking(sibling_role)
    ):
        failures.append(
            "the pair did not reconverge to its launch roles — "
            f"{duty_role} / {sibling_role}"
        )
        raise Abort
    probe = claim_probed(probe_io)
    if claim_reclaim.verdict_owner(probe) != owner_token:
        failures.append(
            "the field's claim stands under token "
            f"{claim_reclaim.verdict_owner(probe)} after the reclaim, "
            f"not the keyed owner's recorded token {owner_token}: "
            f"{probe}"
        )
        raise Abort
    code, verdict, detail = ctl_pair_health(
        args.ctl, monitor_addr(duty_url), monitor_addr(standby_url)
    )
    evidence["pair_health_after"] = (code, verdict)
    if verdict is None:
        raise Inconclusive(
            "the restored pair-health read carried no verdict document "
            "— the released tooling predates the named redundancy-fault "
            "vocabulary",
            f"dcs-ctl pair-health exited {code}: {detail}",
        )
    faults = verdict.get("faults")
    if code != 0 or (isinstance(faults, list) and faults):
        failures.append(
            "the pair-health read still names redundancy faults after "
            f"the reclaim — dcs-ctl exited {code}: {verdict}"
        )
        raise Abort
    evidence["final_tick"] = duty_role.get("tick")
    return ticks


def usurped_claim_reclaim_pass(args, tamper):
    """The usurped-claim-reclaim run: launch the manifest-declared pair
    keyed as the deployment declares it, converge, gate the contract
    surface, stage the seizure through a raw plant-socket attachment,
    assert the narrower verdict and the named pair fault, reclaim the
    field through the pair's own `POST /promote`, and end with the
    launch roles standing. Returns `(digest_entries, evidence,
    failures)`; raises `Inconclusive` where the pinned release
    predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "usurped-claim-reclaim leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = probe_io = foreign = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        # The rig's own client stays read-only: the third-party probes
        # whose fencing verdicts carry the standing claim's declared
        # monitor ride a dedicated attachment on the same plant-socket
        # protocol `dcs-plant-ctl` speaks.
        probe_io = simulate.PlantClient(rig.plant_addr)

        # Phase 1 — the deployed pair, converged on its launch roles.
        converged = rig.converge(failures)
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty": "active",
                "standby": "tracking",
                "keyed": "deployment",
            }
        )

        # Phase 2 — the contract surface the leg's verdict reads.
        floors = {
            "duty": len(pair.get(f"{duty_url}/journal", "GET /journal",
                                 failures)),
            "standby": len(pair.get(f"{standby_url}/journal",
                                     "GET /journal", failures)),
        }
        tokens = declared_keying(args, declared)
        evidence["declared_tokens"] = tokens
        owner_token = gating(rig, probe_io, failures, evidence)
        digest_entries.append(
            {
                "phase": "gate",
                "declared": "keyed",
                "claim": "held",
                "owner": "named",
                "monitor": "owner",
                "proof": "keyed",
            }
        )

        # Phase 3 — the seizure through a raw plant-socket attachment.
        foreign, foreign_url, foreign_monitor = seizure(
            rig, args, probe_io, owner_token, floors, failures, evidence
        )
        digest_entries.append(
            {
                "phase": "seize",
                "attachment": "unkeyed",
                "converged": "tracking",
                "control": evidence["control"],
                "promote": "granted",
                "claim": "foreign",
                "demote": "fenced",
            }
        )

        # Phase 4 — the narrower verdict on both peers, and the named
        # pair fault it reports.
        members, version = narrower_verdict(
            rig, args, foreign_url, foreign_monitor, floors, failures,
            evidence,
        )
        digest_entries.append(
            {
                "phase": "verdict",
                "duty": "usurped",
                "sibling": "usurped",
                "claim": "held",
                "proof": "unprovable",
                "fault": "standby_usurped",
                "members": members,
                "version": version,
            }
        )

        # Phase 5 — the reclaim through the pair's own promotion gate.
        reclaim(
            rig, probe_io, foreign_url, foreign_monitor, owner_token,
            floors, failures, evidence, tamper,
        )
        if tamper is not None:
            return digest_entries, evidence, failures
        digest_entries.append(
            {
                "phase": "reclaim",
                "gate": evidence["reclaim_gate"],
                "promote": "granted",
                "claim": "owner",
                "journal": "foreign-claim-preempted",
                "usurper": "fenced",
                "verdict": evidence["verdict"],
            }
        )

        # Phase 6 — the pair reconverged, launch roles standing.
        ticks = reconverge(
            rig, args, probe_io, owner_token, failures, evidence
        )
        digest_entries.append(
            {
                "phase": "reconverge",
                "ticks": ticks,
                "duty": "active",
                "standby": "tracking",
                "claim": "held",
                "faults": 0,
                "durable": evidence["durable"],
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if probe_io is not None:
            try:
                probe_io.request({"op": "release_writer"})
            except Exception:
                pass
            probe_io.close()
        if foreign is not None:
            pair.stop(foreign)
        if rig is not None:
            rig.close()
    return digest_entries, evidence, failures


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plant-server", required=True)
    parser.add_argument("--controller", required=True)
    parser.add_argument("--ctl", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--tamper",
        choices=["expect-refused-claim"],
        help="doctor the reclaim's own expectation — the promotion "
        "refused with the conditional orphan grant's live-incumbent "
        "field_claim_failed instead of reclaiming the field from the "
        "live writer, so the leg proves its reclaim assertion fires "
        "on the honest grant",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = usurped_claim_reclaim_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "usurped-claim-reclaim: the doctored reclaim wanted the "
                "pair's promotion refused with a field_claim_failed "
                "refusal — an inconclusive run offers the doctored "
                "case no evidence"
            )
            return 1
        # The digest line carries only the stable reason — the run's
        # own verdicts, endpoints, and minted tokens report on stderr,
        # where two identical passes need not share them.
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"usurped-claim-reclaim: inconclusive — {detail}")
        print(f"usurped-claim-reclaim-digest inconclusive — {reason}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"usurped-claim-reclaim: {line}")
        return 1
    for failure in failures:
        eprint(f"usurped-claim-reclaim: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"usurped-claim-reclaim: the {args.tamper} case passed "
                "silently — the leg never noticed the refused reclaim"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"usurped-claim-reclaim-digest {digest} — an unkeyed attachment "
        f"took the keyed pair's field at tick {evidence['seized_at']}, "
        "both keyed peers narrowed their ownerless verdict to usurped "
        "beside field_claim: held and the pair view named it "
        "standby_usurped, POST /promote on the demoted owner took the "
        f"field back at tick {evidence['reclaimed_at']} journaling the "
        "endpoint it was taken from, and the launch roles stand at tick "
        f"{evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
