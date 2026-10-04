#!/usr/bin/env python3
"""The announced-source-verify leg for the reference plant — the
consumer-side proof that an endpoint which announced itself onto a
serving monitor through the documented `GET /checkpoint?peer=` seam is
never adopted as a tracking source and never unblocks the demote guard
(WW-ENG-003, WW-LCM-001 — the announced-source refusal contract #867's
fix settled, mirrored at the customer boundary).

The peer-announce leg (`ci/legs/peer_announce.py`) proves the
announce's *acceptance* half: the serving monitor records an announce
only when it names the pulling connection's own source address, so a
foreign client cannot rewrite the tracking source. This leg proves the
*refusal* half on the same customer-owned pair: a hint that did land —
a foreign endpoint announcing itself from the host's own loopback, the
one source address a peer can genuinely be reached from — is a verify
candidate and never a pull target. The hostile endpoint is this leg's
own loopback `ForeignEndpoint`: it serves one staged checkpoint
document, ledgering every pull that reaches it, and announces itself
onto the field owner's monitor. It is also the shared staging the
sibling announced-hint legs import
(`ci/legs/tracking_source_auth.py`,
`ci/legs/involuntary_demote_verify.py`), so the forge's pull ledger is
one record of what a tracking path may dial at an unproven endpoint.
The run:

- the announced-only window — the declared standby brought up wired at
  an address nothing serves, so no genuine announce ever lands and the
  foreign endpoint's `?peer=` announce is the *only* recorded tracking
  hint. The foreign endpoint stages the field owner's own checkpoint
  bytes replayed with `source_owns_field` rewritten `false` — the
  standby-shaped forged document, replayable from this deployment's own
  public `/checkpoint`. `POST /demote` on the owner must answer the
  named `409 no_tracking_source`: the hinted endpoint cannot produce
  the keyed `line_proof` the pair's shared token arms, so it is a
  candidate that never proves and never unblocks the guard. The owner's
  durable journal must carry the refused probe — a
  `tracking_source_refused` naming the foreign endpoint and the named
  refusal — and *no* `tracking_source_adopted` naming it, while the
  owner keeps its field role;
- the bounded verify pass — the foreign endpoint's ledger must show the
  demote verify reached it at most once per bounded window: a recorded
  hint is pulled to be proved, never followed;
- reconvergence — the tracking standby is then relaunched on the
  owner's real monitor address and the pair converges, so the genuine
  announce joins the bounded set beside the foreign one. The same
  foreign announce re-lands, and `POST /demote` now *is* granted — the
  legitimate follow-peer path is not closed by the refusal — but only
  toward an endpoint that proved the line: the journaled adoption names
  the declared standby's monitor and never the foreign endpoint, the
  demoted owner reconverges `tracking` off its real successor, and the
  forge's ledger never grew a pull the adoption leaned on;
- the restore — the same switch walked back leaves the duty controller
  `active` with the declared standby `tracking`, the launch roles the
  next leg inherits.

The contract postdates some pinned release lines: where the launched
tooling predates it — a served checkpoint without the
`source_owns_field` stamp, the declared `journal_file` persistence
absent, or the refused probe landing no durable `tracking_source_refused`
record at all — the leg reports
`announced-source-verify-digest inconclusive` rather than asserting
until the manifest repins a release carrying the contract.

Usage:

    announced_source_verify.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `announced-source-verify-digest <sha256>` line prints —
the check runs two passes and compares them
(`announced-source-verify-nondeterministic`). A contract violation
reports `announced-source-verify: …` lines on stderr and exits 1 — the
check's `announced-source-verify-failed`. `--tamper expect-adopted`
doctors the leg's expectation to the pre-contract shape — asserting the
`?peer=`-announced forged document unblocks the demote guard and is
adopted, the defect #684 reported — so the leg proves its refusal fires
on the honest `no_tracking_source` rather than passing an unexercised
contract.
"""

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import claim_reclaim
import demote_reconvergence
import pair
import simulate
import tracking_source_fallback


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the forged `?peer=` announce
# unblocks the demote guard — the acceptance-half-only shape the
# refusal half closes — must surface the named diagnostic on the
# honest `no_tracking_source` rather than passing an unexercised
# contract.
LEG = {
    "order": 121,
    "title": "the announced-source-verify leg",
    "passes": "announced-source-verify",
    "tampers": [
        {
            "name": "expect-adopted",
            "passed": "an expect-adopted case passed the announced-source-verify leg",
            "missed": "the expect-adopted case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the announced forged "
                "document to unblock the demote"
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


# The bound on the verify pass the demote guard spends at one hinted
# endpoint: `Monitor::probe_announced_hints` takes newest-announcer
# first, one bounded pull per hint per pass, and re-probes an unchanged
# set only after the four-second `ANNOUNCED_VERIFY_RETRY` window — so a
# recorded hint is pulled to be *proved*, never followed. The leg's
# bound is the whole episode's allowance for the one endpoint it never
# proved: a hint followed as a pull target would dial it once per scan.
VERIFY_PULL_BOUND = 4

# The driven-scan bounds the two phases run: the relaunched standby
# converges `tracking` inside a few pulls, the switch's handover ticks
# carry the demoted owner onto its real successor, and the restore's
# walk back leaves the launch roles standing.
CONVERGE_SCANS = 12
HANDOVER_SCANS = 6


# --------------------------------------------------------------------
# The foreign endpoint the announced-hint legs share. A loopback HTTP
# server in this process serving one staged checkpoint document: it is
# a process outside the pair's keyed line, so it holds no pair token
# and can never produce the `line_proof` the announced verify demands —
# the property under test — while its `?peer=` announce names this
# process's own loopback source address, so it lands on a serving
# monitor exactly as a process outside the deployment would. Every
# pull it serves is appended to the hits ledger: the record of what a
# tracking path dialed at an unproven endpoint.


class _ForeignHandler(BaseHTTPRequestHandler):
    """The forged-checkpoint endpoint's request handler — one staged
    document for every read, every request ledgered."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        endpoint = self.server.endpoint
        with endpoint.lock:
            endpoint.hits.append(
                {
                    "path": self.path,
                    "query": self.path.partition("?")[2],
                    "remote": self.client_address[0],
                }
            )
            document = json.dumps(endpoint.document).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(document)))
        self.end_headers()
        self.wfile.write(document)

    def log_message(self, *args):
        pass


class ForeignEndpoint:
    """The leg's forged-checkpoint endpoint — a loopback server serving
    `document` as its `/checkpoint` answer, ledgering every served pull,
    and announcing itself onto a target monitor through the documented
    `GET /checkpoint?peer=` mechanism. `stage` swaps the served document
    under the lock the handler reads it through, so a rewrite lands
    without a relaunch."""

    def __init__(self, document):
        self.document = document
        self.hits = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ForeignHandler)
        self.server.endpoint = self
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    @property
    def address(self):
        """The endpoint's dialable `host:port` — the address a
        `?peer=` announce names and an adoption would record."""
        return f"127.0.0.1:{self.port}"

    def stage(self, document):
        """Replace the served document."""
        with self.lock:
            self.document = document

    def served_pulls(self, since=0):
        """The ledgered checkpoint pulls — the pulls a tracking path
        dialed at this endpoint from index `since`."""
        with self.lock:
            return [hit for hit in self.hits[since:] if hit["path"].startswith("/checkpoint")]

    def announce(self, url, failures):
        """Record this endpoint on the serving monitor at `url` through
        the documented `?peer=` mechanism, naming the pulling
        connection's own loopback source address so the announce lands
        exactly as a foreign endpoint's would. The checkpoint read
        still answers the serving peer's own document — a recorded
        announce is silent, never an error — and the answer is what
        says the peer is serving."""
        answer = pair.get(
            f"{url}/checkpoint?peer={self.address}",
            f"GET /checkpoint?peer={self.address}",
            failures,
        )
        if not isinstance(answer, dict) or "tick" not in answer:
            failures.append(
                f"the announced checkpoint read answered {answer!r} — "
                "the serving peer served no document to announce beside"
            )
            raise Abort
        return answer

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def forged_document(own):
    """The hostile document derived from a field owner's served
    checkpoint: the victim's own bytes replayed with `source_owns_field`
    rewritten `false` — the standby-shaped forged document every reader
    of this deployment's public `/checkpoint` could derive — with the
    owner-hint and proof decorations stripped. Returns None when the
    served checkpoint is not a field-owning document."""
    if not isinstance(own, dict) or own.get("source_owns_field") is not True:
        return None
    document = json.loads(json.dumps(own))
    document["source_owns_field"] = False
    document.pop("line_owner", None)
    document.pop("line_proof", None)
    return document


def refusals(path, since=0):
    """The `(source, detail)` pairs a `--journal-file`'s records from
    index `since` carry as `tracking_source_refused` — the durable audit
    a refused probe leaves."""
    recorded = []
    for kind, entry in pair.journal_records(path)[since:]:
        if kind != "entry":
            continue
        refused = (entry.get("event") or {}).get("tracking_source_refused")
        if isinstance(refused, dict):
            recorded.append((refused.get("source"), refused.get("detail")))
    return recorded


def adopted_sources(path):
    """The verbatim addresses a `--journal-file`'s
    `tracking_source_adopted` records name — the adoption audit the
    digest's host normalization must not hide. `pair.journal_records`
    elides the port for digest stability, so the adoption's endpoint
    identity is read here instead."""
    return demote_reconvergence.adopted_sources(path)


def adopted_ports(path):
    """The ports the same records name — the port-level form the
    legs' assertions compare against."""
    return [source.rsplit(":", 1)[-1] for source in adopted_sources(path)]


def announced_source_verify_pass(args, tamper):
    """The exercised run: open the announced-only window, stage the
    forged document, announce it, assert `POST /demote` refuses it by
    name inside the bounded verify pass with no adoption journaled and
    the owner keeping its field, then bring the genuine standby up,
    reconverge the pair, and prove the legitimate follow-peer path is
    still open and resolves onto a proven endpoint alone.
    Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "announced-source-verify leg has nothing to exercise"
        )
    manifest, duty_decl, standby_decl = declared
    if rig_journal_absent(declared):
        raise Inconclusive(
            "the manifest's declared pair carries no journal_file — "
            "the durable no-adoption audit is absent"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    foreign = None
    try:
        # Phase 1 — the announced-only window: the declared standby
        # brought up wired at an address nothing serves, so no genuine
        # announce ever lands and the announced set begins empty. A
        # keyed pair cannot demote toward no hint at all, which is
        # precisely what makes the foreign endpoint's landing hint the
        # only thing standing between the guard and its refusal.
        rig = pair.launch_pair(args, declared, tamper="broken-peer-flag")
        duty_url = rig.duty_url
        urls = {
            duty_decl["name"]: duty_url,
            standby_decl["name"]: rig.standby_url,
        }
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        if duty_role.get("role") != "active":
            failures.append(
                "the declared duty controller does not hold the field — "
                f"GET /role answers {duty_role}"
            )
            raise Abort
        own = pair.get(f"{duty_url}/checkpoint", "GET /checkpoint", failures)
        forged = forged_document(own)
        if forged is None:
            raise Inconclusive(
                "the field owner serves no field-owning checkpoint "
                "document — the pinned release predates the "
                "announced-source contract the leg stages a forgery "
                f"of: {own}"
            )
        evidence["forged"] = {
            "tick": forged.get("tick"),
            "source_owns_field": forged.get("source_owns_field"),
            "generation": forged.get("generation"),
        }
        digest_entries.append(
            {
                "phase": "window",
                # The minted generation is the run's own per-boot
                # nonce — two identical passes can never share it, so
                # only the forged document's identifying shape reaches
                # the digest.
                "forged": {
                    "tick": forged.get("tick"),
                    "source_owns_field": forged.get("source_owns_field"),
                },
            }
        )

        foreign = ForeignEndpoint(forged)
        served = foreign.announce(duty_url, failures)
        evidence["announced_at"] = served["tick"]
        digest_entries.append(
            {"phase": "announce", "checkpoint": {"tick": served["tick"]}}
        )

        journal_file = rig.duty_files["journal_file"]
        floor = len(pair.journal_records(journal_file))
        status, body = pair.request(f"{duty_url}/demote", {})
        pulls = foreign.served_pulls()
        role_after = pair.get(f"{duty_url}/role", "GET /role", failures)
        refused = [
            (source, detail)
            for source, detail in refusals(journal_file, floor)
            if str(source).endswith(":" + str(foreign.port))
        ]
        adoptions = adopted_ports(journal_file)
        evidence["refusal"] = {
            "status": status,
            "body": body,
            "verify_pulls": len(pulls),
            "role_after": role_after,
            "refused": [detail for _source, detail in refused],
            "adopted_ports": adoptions,
        }
        if tamper == "expect-adopted":
            # The doctored expectation: the `?peer=`-announced forged
            # document unblocks the guard. The honest run's named
            # refusal is the evidence the doctor cannot satisfy, so
            # whichever way the demote answered the pass fails naming
            # it.
            failures.append(
                "the doctored expectation wanted the announced forged "
                f"document to unblock the demote — POST /demote "
                f"answered {status} {body} instead"
            )
            raise Abort
        if status != 409:
            failures.append(
                f"POST /demote on the field owner answered {status} "
                f"{body}, expected the named 409 no_tracking_source "
                "refusal"
            )
        if body != "no_tracking_source":
            failures.append(
                f"the refused demote answered {body!r} — the guard's "
                "named refusal is no_tracking_source"
            )
        if not pulls:
            failures.append(
                "the foreign endpoint's ledger recorded no checkpoint "
                "pull — the refusal never reached the staged document, "
                "so the verify pass was not exercised"
            )
        if len(pulls) > VERIFY_PULL_BOUND:
            failures.append(
                f"the demote verify spent {len(pulls)} pulls at one "
                "unproven endpoint — a recorded hint is a verify "
                "candidate, never a pull target"
            )
        if role_after.get("role") != "active":
            failures.append(
                "the field owner stopped owning the field on the "
                f"refused demote — GET /role answers {role_after}"
            )
        if str(foreign.port) in adoptions:
            failures.append(
                "the journaled tracking_source_adopted records name the "
                f"foreign endpoint's port {foreign.port} — the forged "
                "announce was adopted"
            )
        if not refused:
            raise Inconclusive(
                "the refused probe journaled no tracking_source_refused "
                "record naming the foreign endpoint — the pinned "
                "release predates the durable announced-source audit "
                "the leg reads"
            )
        digest_entries.append(
            {
                "phase": "refusal",
                "status": status,
                "body": body,
                "verify_pulls": len(pulls),
                "role_after": role_after.get("role"),
                "refused": sorted(detail for _s, detail in refused),
                "adopted_foreign": str(foreign.port) in adoptions,
            }
        )

        # Phase 2 — reconvergence: the declared standby relaunched on
        # the owner's real monitor address, so its tracking pulls
        # announce the genuine address beside the foreign one and the
        # pair converges on the declared wiring.
        pair.stop(rig.standby)
        rig.standby, standby_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            duty_url.removeprefix("http://"),
            rig.standby_files,
            pair_token=pair.PAIR_TOKEN,
        )
        rig.standby_url = standby_url
        if standby_url is None:
            failures.append(
                "the relaunched declared standby exited at startup: "
                f"{'; '.join(preamble) or 'no diagnostic'}"
            )
            raise Abort
        urls[standby_decl["name"]] = standby_url
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

        # The same foreign announce rejoins the bounded set beside the
        # genuine one; the demote must now be granted — the refusal
        # closed the forged candidate, not the legitimate follow-peer
        # path — but only toward an endpoint that proved the line.
        foreign.announce(duty_url, failures)
        pulls_before = len(foreign.served_pulls())
        decisive = {}

        def audit():
            """The decisive read: the demote's own answer and the
            adoption it journaled, taken at the last moment before the
            demoted owner's tracking pulls begin."""
            decisive["demoted_role"] = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            decisive["adopted_ports"] = adopted_ports(journal_file)
            decisive["foreign_pulls"] = (
                len(foreign.served_pulls()) - pulls_before
            )

        switched = rig.switch(
            duty_url,
            standby_url,
            failures,
            handover=HANDOVER_SCANS,
            demote_what="the field owner",
            promote_what="the converged declared standby",
            after_promote=audit,
        )
        demoted_role = switched["demoted_role"]
        promoted_role = switched["promoted_role"]
        adopted = decisive["adopted_ports"]
        evidence["switch"] = {
            "demote": switched["demote"],
            "promote": switched["promote"],
            "handover": switched["ticks"],
            "demoted_role": demoted_role,
            "promoted_role": promoted_role,
            "adopted_ports": adopted,
            "foreign_pulls": decisive["foreign_pulls"],
        }
        if str(foreign.port) in adopted:
            failures.append(
                "the demote journaled a tracking_source_adopted naming "
                f"the foreign endpoint's port {foreign.port} — the "
                "forged announce was adopted beside the proven one"
            )
        if tracking_source_fallback.monitor_port(standby_url) not in adopted:
            failures.append(
                "the demote journaled no tracking_source_adopted naming "
                f"the declared standby's monitor — the adoptions are {adopted}"
            )
        if decisive["foreign_pulls"] > VERIFY_PULL_BOUND:
            failures.append(
                "the demote verify spent "
                f"{decisive['foreign_pulls']} pulls at the foreign "
                "endpoint once a proven hint stood beside it — a "
                "recorded hint is a verify candidate, never a pull "
                "target"
            )
        digest_entries.append(
            {
                "phase": "switch",
                "demote": switched["demote"],
                "promote": switched["promote"],
                "ticks": switched["ticks"],
                "demoted_role": demoted_role,
                "promoted_role": promoted_role,
                "adopted": [
                    tracking_source_fallback.peer_kind(urls, source)
                    for source in adopted_sources(journal_file)
                ],
                "foreign_pulls": decisive["foreign_pulls"],
            }
        )

        # Phase 3 — the restore: the same switch walked back leaves the
        # manifest's declared launch arrangement standing for the next
        # leg.
        restored = rig.switch(
            standby_url,
            duty_url,
            failures,
            handover=HANDOVER_SCANS,
            demote_what="the promoted declared standby",
            promote_what="the reconverged duty controller",
        )
        final = rig.converge(failures, count=CONVERGE_SCANS)
        if final["duty_role"].get("role") != "active" or not claim_reclaim.tracking(
            final["standby_role"]
        ):
            failures.append(
                "the pair never restored its launch roles — the duty "
                f"controller answers {final['duty_role']} and the "
                f"declared standby {final['standby_role']}"
            )
        evidence["restored"] = restored["ticks"][-1]
        digest_entries.append(
            {
                "phase": "restore",
                "demote": restored["demote"],
                "promote": restored["promote"],
                "ticks": restored["ticks"],
                "duty_role": final["duty_role"],
                "standby_role": final["standby_role"],
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


def rig_journal_absent(declared):
    """Whether the declared pair carries no durable journal at all —
    the audit this leg reads has nowhere to land."""
    _manifest, duty_decl, standby_decl = declared
    return not any(entry.get("journal_file") for entry in (duty_decl, standby_decl))


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
        "asserting the ?peer=-announced forged document unblocks the "
        "demote guard and is adopted — so the pass must fail naming "
        "the refusal it observed",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = announced_source_verify_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "announced-source-verify: an inconclusive run under "
                f"the {args.tamper} doctor offers the doctored case "
                "no evidence"
            )
            return 1
        eprint(f"announced-source-verify: inconclusive — {inconclusive}")
        print(f"announced-source-verify-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"announced-source-verify: {line}")
        return 1
    for failure in failures:
        eprint(f"announced-source-verify: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"announced-source-verify: the {args.tamper} case "
                "passed silently — the leg never noticed the forged "
                "announce unblocking the demote"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"announced-source-verify-digest {digest} — the announced-only "
        f"window's forged document announced at tick "
        f"{evidence['announced_at']} was refused no_tracking_source "
        f"after {evidence['refusal']['verify_pulls']} bounded verify "
        f"pull, the pair reconverged by tick {evidence['converged']}, "
        f"the switch's handover ran to tick "
        f"{evidence['switch']['handover'][-1]} and the launch roles "
        f"restored at tick {evidence['restored']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
