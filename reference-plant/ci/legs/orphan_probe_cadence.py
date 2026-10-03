#!/usr/bin/env python3
"""The orphan-probe-cadence leg for the reference plant — the
consumer-side proof that on the manifest-declared *unkeyed* pair a
held foreign claim declaring an undialable monitor does not collapse
the orphaned peers' paced scan cadence: the tracking-source
resolution probe the #1256 fix bounds costs one bounded pull burst
per probe window, not one per orphaned scan (WW-ENG-003, WW-LCM-001 —
the rig's orphan-probe-cadence contract mirrored at the customer
boundary, on the paced deployment shape the finding reproduced).

The foreign-claim-release leg (`ci/legs/foreign_claim_release.py`)
pins the held monitor-less claim's `unsynchronized` window and its
release resolutions on the driven pair; the orphan-retarget-journal
leg pins the resolution's durable record. This leg pins the claim's
cost *while it stands*: a foreign `claim_writer` declaring a monitor
endpoint that accepts the dial but never answers — the dead-monitor
shape the finding measured — leaves every orphaned apply probing the
claim-declared endpoint first. On the pre-contract build that probe
ran synchronously inside each orphaned scan cycle, one checkpoint-
pull bound per scan, so the paced cadence collapsed (~1 scan per
second at the deployment's `--scan-ms 100`) and every cycle fed
`io_health.scan_overruns`. The contract caches the refused candidate
set and re-probes only when it changes or the retry window elapses —
the leg measures the served evidence of exactly that:

- the pair launches the way the deployment declares it — paced at
  the manifest's `--scan-ms`, unkeyed, each controller bound on its
  declared wildcard listen host — and settles `active`/`tracking`.
  The manifest's declared `failover_budget` stays unarmed for this
  leg: an armed gate retires the orphaned peer's tracking-source
  probe after `budget` applies — each later orphaned cycle is a
  refused promotion attempt rather than a probe, so the contract's
  cost surface leaves the scan path entirely and pre-contract and
  bounded builds become indistinguishable in-window — and its
  re-armed claim probe at the release would race the ex-owner's
  bound reclaim. The armed gate's refused-promotion behavior is the
  failover legs' own contract; this leg needs the orphaned-apply
  probe held reachable for the claim's whole standing window;
- a dedicated plant-socket attachment places the foreign claim with
  the silent endpoint declared as its monitor; the superseded owner
  demotes in place and both peers read orphaned: the fenced ex-owner
  `standby`/`unsynchronized`, the tracked sibling `standby`/
  `orphaned` on the ownerless line;
- the standing window: across a span covering at least one probe
  retry boundary, each peer's served `publication.published` — the
  one counter a completed paced scan mints — and run tick must
  advance within the declared bound of the scan cadence, while the
  fencing verdict keeps naming the foreign claim's dead monitor;
  and the served `io_health.scan_overruns` must show the bounded
  probe's own cost — a handful for the passes the retry bound
  paid, never the per-scan collapse the finding measured — and
  each peer's journal must carry the refused-probe record naming
  the dead endpoint, the claim's monitor verifiably inside its
  probe set. A collapse, and a window paying nothing or journaling
  no refusal because the claim's monitor never entered the probe's
  candidate set, are the pinned release predating the contract —
  not a contract violation;
- the release resolves through the ex-owner's recorded reclaim —
  the unattended `standby → promoting → active` walk under the
  `reclaim` origin — the pair reconverging to its launch roles with
  the claim re-seated under the launch owner's token.

The contract postdates the early release line: where the launched
tooling predates it — no owner-token claim line, an unanswered claim
verb, a fencing verdict naming no owner or no declared monitor, a
snapshot without the `io_health`/`publication` counters, a role
report without the sync vocabulary, or a journal carrying no
refused-probe audit — or where the run's own reading is the collapse
the contract closed — the leg reports
`orphan-probe-cadence-digest inconclusive` rather than asserting
until the manifest repins a release carrying it.

Usage:

    orphan_probe_cadence.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `orphan-probe-cadence-digest <sha256>` line prints —
the check runs two passes and compares them
(`orphan-probe-cadence-nondeterministic`). A contract violation
reports `orphan-probe-cadence: …` lines on stderr and exits 1 — the
check's `orphan-probe-cadence-failed`. `--tamper expect-collapse`
doctors the leg's expectation to the defect's shape — asserting the
standing dead-monitor claim may collapse the peers' paced cadence,
the reproduction the contract closed — so the leg proves its cadence
assertion fires on the honest held window rather than passing an
unexercised contract.
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import claim_reclaim
import failover
import pair
import simulate
import stranded_rejoin


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the standing dead-monitor claim
# may collapse the orphaned peers' paced cadence — the pre-contract
# reproduction the bounded probe closed — must surface the named
# diagnostic on the honest held window rather than passing an
# unexercised contract.
LEG = {
    "order": 660,
    "title": "the orphan-probe-cadence leg",
    "passes": "orphan-probe-cadence-leg",
    "tampers": [
        {
            "name": "expect-collapse",
            "passed": "an expect-collapse case passed the orphan-probe-cadence leg",
            "missed": "the expect-collapse case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the standing dead-monitor claim collapsing the orphaned peers' paced cadence"
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
    reported on stderr only, where ephemeral verdicts, endpoints,
    and measured counters belong."""


# The deployment's declared scan period — the manifest instantiates
# `--scan-ms 100` on both controllers — and the wall-clock bounds
# each phase polls: the paced settle windows carry generous
# deadlines since the contract they wait on is a bound, not a tick
# count.
SCAN_MS = 100
CONVERGE_BOUND = 25.0
DEMOTE_BOUND = 5.0
ORPHAN_BOUND = 5.0
RESOLVE_BOUND = 15.0
RECONVERGE_BOUND = 15.0
POLL_INTERVAL = 0.05

# The claim's standing window — long enough to span at least one
# probe retry boundary, so the bounded contract is exercised rather
# than only its cache hit — and the cadence evidence bound: each
# peer's served `publication.published` count of completed paced
# scans and its run tick must advance at least this fraction of the
# window's declared scan count, while `io_health.scan_overruns`
# growth shows the bounded probe passes the retry window paid —
# never the per-scan collapse — the journaled `tracking_source_refused`
# naming the dead endpoint on each peer carrying the durable half
# of the proof that the probe path was exercised at all.
HOLD_SECONDS = 8.0
CADENCE_FRACTION = 0.5
OVERRUN_SLACK = 4

# The dedicated attachment's foreign owner token — a small fixed
# value that cannot collide with a controller's per-process minted
# token, distinct from the tokens the other legs stage.
FOREIGN_CLAIM = 0xF125


def drain_stderr(process):
    """Keep a paced child's stderr drained past launch — a consumer
    that stops reading a full pipe must never pace the scan cycle
    it measures, so the post-preamble lines drain on a daemon
    thread."""
    def run():
        for _line in process.stderr:
            pass
    threading.Thread(target=run, daemon=True).start()


def spawn_paced(binary, model, plant_addr, standby, files,
                auto_promote=None, listen="127.0.0.1:0", bound=None):
    """Spawn `dcs-controller <model> --remote … --scan-ms` for one
    pair peer — the manifest's paced deployment shape rather than
    the driven `POST /scan` run `pair.spawn_peer` builds: `standby`
    the manifest's tracking wiring (None on the field owner),
    `auto_promote` an optional failover budget (None leaves
    promotion manual — the shape this leg needs, an armed gate
    retiring the orphaned peer's probe path into refused
    promotions), `files` the controller's declared
    persistence paths under the rig's scratch directory, `listen`
    the `--listen` bind (the declared wildcard host on a
    runner-assigned port), `bound` an optional list the verbatim
    bound address is appended to. The per-scan snapshot stream goes
    to devnull — its line count is wall-clock data two identical
    passes cannot share — while the stderr preamble reads until the
    `listening on` report and then keeps draining off-thread.
    Returns `(process, monitor_url, preamble)`; `monitor_url` is
    the dialable form of the reported bind, or None when the
    process exits first, the preamble then carrying the startup
    refusal's stderr lines."""
    argv = [
        binary,
        model,
        "--remote",
        plant_addr,
        "--scan-ms",
        str(SCAN_MS),
        "--listen",
        listen,
    ]
    if standby is not None:
        argv += ["--standby", standby]
    if auto_promote is not None:
        argv += ["--auto-promote", str(auto_promote)]
    for field, flag in (
        ("state_file", "--state-file"),
        ("journal_file", "--journal-file"),
        ("history_file", "--history-file"),
    ):
        if files.get(field) is not None:
            argv += [flag, files[field]]
    process = subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    preamble = []
    for line in process.stderr:
        line = line.strip()
        if "listening on" in line:
            raw = line.rsplit(None, 1)[-1]
            if bound is not None:
                bound.append(raw)
            drain_stderr(process)
            return process, "http://" + pair.dialable(raw), preamble
        preamble.append(line)
    process.wait(timeout=10)
    return process, None, preamble


def launch_paced_unkeyed(args, declared):
    """The manifest-declared pair launched the way the deployment
    declares it — paced at `--scan-ms`, no `--pair-token`, each
    controller bound on its declared wildcard listen host with a
    runner-assigned port — save the standby's declared
    `failover_budget`, left unarmed here: an armed gate retires the
    orphaned peer's tracking-source probe into refused promotions
    after `budget` applies, so the contract's per-window cost would
    leave the scan path for the standing window, and its re-armed
    conditional claim would race the ex-owner's bound reclaim at
    the release. The shared `pair.launch_pair` runs driven scans on
    a keyed pair — neither the cadence surface nor the claim shape
    this leg exercises — so the leg composes the same spawn
    sequence itself. Returns the PairRig."""
    rig = pair.PairRig(declared)
    try:
        rig.plant, rig.plant_addr = pair.spawn_plant(
            args.plant_server, args.model, args.dynamics
        )
        rig.plant_io = simulate.PlantClient(rig.plant_addr)
        rig.duty, rig.duty_url, rig.duty_preamble = spawn_paced(
            args.controller,
            args.model,
            rig.plant_addr,
            None,
            rig.duty_files,
            listen=pair.listen_bind(rig.duty_decl),
            bound=rig.duty_bound,
        )
        if rig.duty_url is None:
            raise Abort(
                f"the duty controller {rig.duty_decl['name']} exited "
                f"at startup: "
                f"{'; '.join(rig.duty_preamble) or 'no diagnostic'}"
            )
        rig.standby, rig.standby_url, rig.standby_preamble = (
            spawn_paced(
                args.controller,
                args.model,
                rig.plant_addr,
                rig.duty_url.removeprefix("http://"),
                rig.standby_files,
                listen=pair.listen_bind(rig.standby_decl),
                bound=rig.standby_bound,
            )
        )
        if rig.standby_url is None:
            raise Abort(
                f"the standby controller {rig.standby_decl['name']} "
                "exited at startup: "
                f"{'; '.join(rig.standby_preamble) or 'no diagnostic'}"
            )
    except Exception:
        rig.close()
        raise
    return rig


def role(url):
    """`GET /role` answering None on transport error — a missed
    answer inside a poll window is data, not a harness failure."""
    return claim_reclaim.try_role(url)


def wait_role(url, accept, deadline, failures, what):
    """Poll `GET /role` until a report satisfies `accept` or the
    deadline lapses — the paced pair's settle is wall-clock, so the
    leg waits where a driven leg would tick."""
    end = time.monotonic() + deadline
    report = None
    while time.monotonic() < end:
        report = role(url)
        if report is not None and accept(report):
            return report
        time.sleep(POLL_INTERVAL)
    failures.append(f"{what}: the last GET /role answered {report}")
    raise Abort


def snapshot(url, failures):
    """One `GET /snapshot` read — the served counters the cadence
    assertions measure."""
    return pair.get(f"{url}/snapshot", "GET /snapshot", failures)


def served_tick(doc):
    """The run tick a served snapshot reports."""
    return (doc or {}).get("tick")


def overruns(doc):
    """The served `io_health.scan_overruns` counter, or None where
    the section is absent."""
    return ((doc or {}).get("io_health") or {}).get("scan_overruns")


def published(doc):
    """The served `publication.published` counter — one minted per
    completed paced scan — or None where the section is absent."""
    return ((doc or {}).get("publication") or {}).get("published")


def monitor_addr(url):
    """The peer's dialable monitor address as `host:port`."""
    return url.removeprefix("http://")


def monitor_kind(declared, owner_addr):
    """Classify a claim-declared monitor: `wildcard` for an
    unspecified bind address, `owner` for the claiming peer's own
    dialable monitor, `foreign` for anything else. Only `owner` is
    a contract answer."""
    text = str(declared)
    host = text.rsplit(":", 1)[0]
    if host in ("0.0.0.0", "::", "[::]"):
        return "wildcard"
    if text == owner_addr:
        return "owner"
    return "foreign"


def dead_listener():
    """The undialable monitor the foreign claim declares: a bound
    loopback listener whose connects land in its backlog and are
    never answered — the silent endpoint each probe waits the full
    pull bound on."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    return listener


def orphan_probe_cadence_pass(args, tamper):
    """The orphan-probe-cadence run: launch the paced unkeyed
    declared-binds pair, converge, gate the contract surface, then
    the dead-monitor claim, the bounded cadence window, the
    release, the reclaim resolution, and the launch-role restore.
    Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the
    contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "orphan-probe-cadence leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    rig = verdict_io = foreign_io = dead = None
    try:
        rig = launch_paced_unkeyed(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        # The claim's attachments: `verdict_io` runs the third-party
        # mutation probes whose fencing verdicts carry the standing
        # claim's declared monitor, `foreign_io` holds the
        # dead-monitor foreign claim the window asserts.
        verdict_io = simulate.PlantClient(rig.plant_addr)
        foreign_io = simulate.PlantClient(rig.plant_addr)

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

        # Phase 2 — convergence and the contract surface: the paced
        # pair settles `active`/`tracking`, the launched active's
        # recorded claim token, the standing claim fencing a
        # third-party mutation while naming its owner and its
        # declared dialable monitor, and the served counters the
        # cadence window reads. Each absence is the release
        # predating the contract, never a violation.
        duty_role = wait_role(
            duty_url,
            lambda report: report.get("role") == "active",
            CONVERGE_BOUND,
            failures,
            "the launched duty never reported active",
        )
        standby_role = wait_role(
            standby_url,
            claim_reclaim.tracking,
            CONVERGE_BOUND,
            failures,
            "the tracking peer never reported tracking",
        )
        digest_entries.append(
            {
                "phase": "converge",
                "binds": binds,
                "duty": duty_role.get("role"),
                "standby": stranded_rejoin.sync_kind(standby_role),
            }
        )
        owner_token = failover.owner_token(rig.duty_preamble)
        if owner_token is None:
            raise Inconclusive(
                "the launched active recorded no claim line — the "
                "pinned release claims only on promotion, predating "
                "the claim lifecycle the probe contract rides on"
            )
        probe0 = verdict_io.request({"op": "step", "dt": 0})
        evidence["probe0"] = probe0
        if not claim_reclaim.mutation_fenced(probe0):
            failures.append(
                "the field held no writer claim after convergence — "
                f"a third-party probe answered {probe0}, so the leg "
                "has no standing claim to lose"
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
        declared0 = stranded_rejoin.verdict_monitor(probe0)
        if declared0 is None:
            raise Inconclusive(
                "the standing claim declares no monitor — the "
                "pinned release predates the claim-declared monitor "
                "the probe resolution reads",
                f"the standing fencing verdict was {probe0}",
            )
        kind0 = monitor_kind(declared0, monitor_addr(duty_url))
        if kind0 == "wildcard":
            raise Inconclusive(
                "the standing claim declares its wildcard bind "
                "verbatim — the pinned release predates the "
                "claim-monitor normalization",
                f"the declared monitor was {declared0}",
            )
        if kind0 != "owner":
            failures.append(
                f"the standing claim declares monitor {declared0} "
                "— not the field owner's dialable monitor "
                f"{monitor_addr(duty_url)}"
            )
            raise Abort
        doc = pair.get(
            f"{duty_url}/checkpoint", "GET /checkpoint", failures
        )
        if "source_owns_field" not in doc or "line_owner" not in doc:
            raise Inconclusive(
                "the served checkpoint carries no field-ownership "
                "stamps — the pinned release predates the "
                "field-arbitrated monitor contract",
                f"the checkpoint serves {sorted(doc)}",
            )
        gate0 = snapshot(duty_url, failures)
        gate1 = snapshot(standby_url, failures)
        if overruns(gate0) is None or overruns(gate1) is None:
            raise Inconclusive(
                "the served snapshot carries no io_health."
                "scan_overruns — the pinned release predates the "
                "counter the paced-cadence contract reports through",
                f"the duty snapshot serves {sorted(gate0)}",
            )
        if published(gate0) is None or published(gate1) is None:
            raise Inconclusive(
                "the served snapshot carries no publication."
                "published count — the pinned release predates the "
                "publication store the paced cadence measures "
                "through",
                f"the duty snapshot serves {sorted(gate0)}",
            )
        digest_entries.append(
            {
                "phase": "gate",
                "probe": "fenced",
                "owner": "named",
                "monitor": kind0,
                "counters": "served",
            }
        )

        # Phase 3 — the dead-monitor foreign claim: a dedicated
        # attachment's `claim_writer` under the leg's fixed token,
        # `controller: true` marking the hold a foreign
        # *controller's* — the live incumbent no conditional grant
        # may lift, so the orphaned peers hold their posture through
        # the claim's standing window, the shape the finding
        # measured — with `monitor` declaring the silent endpoint
        # the preemption's fencing verdict must carry back.
        dead = dead_listener()
        dead_addr = "%s:%d" % dead.getsockname()[:2]
        claim = foreign_io.request(
            {
                "op": "claim_writer",
                "owner": FOREIGN_CLAIM,
                "controller": True,
                "monitor": dead_addr,
            }
        )
        evidence["claim"] = claim
        if claim_reclaim.unsupported_verb(claim):
            raise Inconclusive(
                "claim_writer answered invalid_request — the pinned "
                "release predates the claim lifecycle the leg "
                "stages",
                f"claim_writer answered {claim}",
            )
        result = claim.get("result")
        if result == "claimed_shared":
            raise Abort(
                "the foreign claim answered claimed_shared — a live "
                "attachment already holds the leg's fixed token "
                f"{FOREIGN_CLAIM:#x}: {claim}"
            )
        if result != "done":
            raise Inconclusive(
                f"the foreign claim answered {result} — the "
                "consumer harness admits no unconditional "
                "preemption lever, or the pinned plant predates it",
                f"claim_writer answered {claim}",
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
        declared_monitor = stranded_rejoin.verdict_monitor(seized)
        if declared_monitor is None:
            raise Inconclusive(
                "the dead-monitor claim declares no monitor in the "
                "fencing verdict — the pinned release predates the "
                "claim-declared monitor the orphan probe reads",
                f"the fencing verdict was {seized}",
            )
        if declared_monitor != dead_addr:
            failures.append(
                "the foreign claim's declared monitor reads "
                f"{declared_monitor} in the fencing verdict — the "
                f"dead endpoint {dead_addr} it declared was "
                "expected"
            )
            raise Abort

        # Phase 4 — the fenced demotion and the orphan posture: the
        # superseded owner's paced scans walk it
        # `active → demoting → standby` under the `fenced` origin
        # beside one `field_claim_lost` attributed to the foreign
        # token, and the tracked sibling's applies of the ownerless
        # line report `orphaned` — the posture whose resolution
        # probes the contract bounds.
        floor = len(pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        ))
        demoted = wait_role(
            duty_url,
            lambda report: report.get("role") == "standby",
            DEMOTE_BOUND,
            failures,
            "the superseded owner never demoted under the held "
            "foreign claim",
        )
        evidence["demoted"] = demoted.get("role")
        journal = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        losses = stranded_rejoin.lost_entries(journal[floor:])
        evidence["losses"] = losses
        if not losses:
            raise Inconclusive(
                "the fenced demotion left no field_claim_lost on "
                "the ex-owner's journal — the pinned release "
                "predates the loss record",
                f"the added journal carried {journal[floor:]}",
            )
        if len(losses) != 1:
            failures.append(
                "expected exactly one field_claim_lost above the "
                f"journal floor, found {len(losses)} — one record "
                "per held claim, not one per fenced write"
            )
            raise Abort
        if "claimant" not in losses[0][1]:
            raise Inconclusive(
                "the journaled fencing loss names no claimant — "
                "the pinned release predates the attribution "
                "contract",
                f"the journaled loss was {losses}",
            )
        if losses[0][1].get("claimant") != FOREIGN_CLAIM:
            failures.append(
                "the journaled fencing loss attributes the "
                "takeover to "
                f"{losses[0][1].get('claimant')}, not the foreign "
                f"token {FOREIGN_CLAIM:#x}"
            )
            raise Abort
        walk = stranded_rejoin.role_walk(journal[floor:])
        if any(origin is None for _from, _to, origin in walk):
            raise Inconclusive(
                "the fenced demote walk carries no switch origin — "
                "the pinned release predates the attribution "
                "fields",
                f"the role walk was {walk}",
            )
        if not stranded_rejoin.demote_walk(journal[floor:]):
            failures.append(
                "the superseded owner's journaled role walk is "
                f"{walk}, expected the fenced "
                "active → demoting → standby"
            )
            raise Abort
        orphaned = None
        seen = []
        end_at = time.monotonic() + ORPHAN_BOUND
        while time.monotonic() < end_at:
            report = role(standby_url)
            if report is not None:
                kind = stranded_rejoin.sync_kind(report)
                seen.append(kind)
                if (
                    report.get("role") == "standby"
                    and kind == "orphaned"
                ):
                    orphaned = report
                    break
            time.sleep(POLL_INTERVAL)
        evidence["orphaned"] = seen
        if orphaned is None:
            if not seen or any(kind == "missing" for kind in seen):
                raise Inconclusive(
                    "the tracking peer's role report carries no "
                    "sync vocabulary — the pinned release predates "
                    "the contract's orphaned verdict",
                    f"the sibling's reports were {seen}",
                )
            raise Inconclusive(
                "the tracking peer never reported the orphaned "
                "line under the held dead-monitor claim — the "
                "pinned release predates the ownerless-line "
                "verdict the probe cadence contract rides",
                f"the sibling's sync readings were {seen}",
            )
        ex_role = role(duty_url)
        if ex_role is None:
            failures.append(
                "the fenced ex-owner's monitor stopped answering "
                "GET /role under the held dead-monitor claim"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "claim",
                "seized": "fenced",
                "monitor": "declared",
                "duty": (
                    demoted.get("role")
                    + "/"
                    + stranded_rejoin.sync_kind(ex_role)
                ),
                "sibling": orphaned.get("role")
                + "/"
                + stranded_rejoin.sync_kind(orphaned),
            }
        )

        # Phase 5 — the standing window: while the dead-monitor
        # claim stands, each peer's `publication.published` — one
        # per completed paced scan — and run tick must advance
        # within the declared bound of the scan cadence, while the
        # served `io_health.scan_overruns` growth stays inside the
        # slack the bounded probe passes legitimately cost — and
        # each peer's journal carries the refused probe naming the
        # dead endpoint. A collapse is the finding's own
        # reproduction: the pinned release predates the bounded
        # probe contract.
        start = {
            "duty": snapshot(duty_url, failures),
            "sibling": snapshot(standby_url, failures),
        }
        started = time.monotonic()
        time.sleep(HOLD_SECONDS)
        elapsed = time.monotonic() - started
        end = {
            "duty": snapshot(duty_url, failures),
            "sibling": snapshot(standby_url, failures),
        }
        verdict = verdict_io.request({"op": "step", "dt": 0})
        evidence["held_verdict"] = verdict
        if not claim_reclaim.mutation_fenced(verdict) or (
            claim_reclaim.verdict_owner(verdict) != FOREIGN_CLAIM
        ):
            failures.append(
                "the standing claim moved off the foreign token "
                f"during the cadence window: {verdict}"
            )
            raise Abort
        if stranded_rejoin.verdict_monitor(verdict) != dead_addr:
            failures.append(
                "the standing claim's declared monitor moved off "
                f"the dead endpoint during the cadence window: "
                f"{verdict}"
            )
            raise Abort
        end_duty = role(duty_url)
        end_sibling = role(standby_url)
        evidence["held_roles"] = {
            "duty": end_duty,
            "sibling": end_sibling,
        }
        if end_duty is None or end_duty.get("role") != "standby" or (
            stranded_rejoin.sync_kind(end_duty) != "unsynchronized"
        ):
            failures.append(
                "the fenced ex-owner reported "
                f"{end_duty} while the dead-monitor claim stood — "
                "`standby`/`unsynchronized` is the only honest "
                "report for that window"
            )
            raise Abort
        if end_sibling is None or (
            end_sibling.get("role") != "standby"
        ) or stranded_rejoin.sync_kind(end_sibling) != "orphaned":
            failures.append(
                "the tracked sibling reported "
                f"{end_sibling} while the dead-monitor claim "
                "stood — `standby`/`orphaned` is the only honest "
                "report for that window"
            )
            raise Abort
        expected = elapsed * 1000.0 / SCAN_MS
        bound = expected * CADENCE_FRACTION
        measured = {}
        collapsed = []
        for peer in ("duty", "sibling"):
            if (
                published(end[peer]) is None
                or served_tick(end[peer]) is None
                or overruns(end[peer]) is None
            ):
                failures.append(
                    f"the {peer} peer's served cadence counters "
                    "vanished mid-window — the contract's "
                    "measurement surface is gone: "
                    f"{end[peer]}"
                )
                raise Abort
            advance = {
                "published": published(end[peer]) - published(start[peer]),
                "tick": served_tick(end[peer]) - served_tick(start[peer]),
                "overruns": overruns(end[peer]) - overruns(start[peer]),
            }
            measured[peer] = advance
            if (
                advance["published"] < bound
                or advance["tick"] < bound
            ):
                collapsed.append(
                    f"{peer} advanced published="
                    f"{advance['published']} tick={advance['tick']} "
                    f"against {expected:.0f} declared scans"
                )
        evidence["window"] = {
            "expected_scans": round(expected, 1),
            "measured": measured,
        }
        if collapsed:
            raise Inconclusive(
                "the standing dead-monitor claim collapsed the "
                "orphaned peers' paced cadence — the pinned "
                "release predates the bounded probe contract the "
                "leg exercises",
                "; ".join(collapsed),
            )
        over_growth = {
            peer: advance["overruns"]
            for peer, advance in measured.items()
        }
        # The durable half of the probe's exercise: each peer's
        # journal must carry a `tracking_source_refused` naming the
        # claim's dead monitor — the orphaned sibling's resolution
        # pass and the fenced ex-owner's claimed-monitor pass both
        # auditing the endpoint that never answered — so a window
        # whose cost bound held only because the dead endpoint never
        # entered the probe's candidate set reports inconclusive.
        probed = {}
        for peer, url in (("duty", duty_url), ("sibling", standby_url)):
            journal = pair.get(
                f"{url}/journal", "GET /journal", failures
            )
            probed[peer] = [
                record
                for entry in journal
                for record in [
                    (entry.get("event") or {}).get(
                        "tracking_source_refused"
                    )
                ]
                if (record or {}).get("source") == dead_addr
            ]
        evidence["probe_refusals"] = {
            peer: len(records) for peer, records in probed.items()
        }
        if not all(probed.values()):
            raise Inconclusive(
                "the claim's dead monitor never entered a peer's "
                "probe set — no journaled tracking_source_refused "
                "names the endpoint — the pinned release predating "
                "the candidate ordering the contract bounds",
                f"dead endpoint {dead_addr}, per-peer refusals "
                f"{evidence['probe_refusals']}",
            )
        unpaid = [
            peer
            for peer, growth in over_growth.items()
            if growth < 1
        ]
        if unpaid:
            raise Inconclusive(
                "the dead-monitor claim's bounded probe never paid "
                "its declared cost inside the standing window — the "
                "pinned release predating the per-window re-probe "
                "the contract bounds",
                f"per-peer overrun growth {over_growth}, "
                f"unpaid: {unpaid}",
            )
        if any(growth > OVERRUN_SLACK for growth in over_growth.values()):
            failures.append(
                "the standing dead-monitor claim's probe cost grew "
                "the served io_health.scan_overruns past the "
                f"declared bound — per-peer growth {over_growth} "
                f"against the {OVERRUN_SLACK}-scan slack the "
                "bounded probe window costs"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "held",
                "verdict": "foreign",
                "monitor": "dead",
                "duty": "standby/unsynchronized",
                "sibling": "standby/orphaned",
                "cadence": "paced",
                "overruns": "bounded",
            }
        )

        # The doctored case — the leg asserting the defect's own
        # shape: the standing dead-monitor claim may collapse the
        # orphaned peers' paced cadence. The honest held window
        # must fail it.
        if tamper == "expect-collapse":
            failures.append(
                "the doctored expectation wanted the standing "
                "dead-monitor claim collapsing the orphaned "
                "peers' paced cadence — the honest run held the "
                "paced tick rate and the overrun counters at "
                "baseline"
            )
            raise Abort

        # Phase 6 — the release and the resolution: the attachment
        # holding the claim hands it back, and the fenced ex-owner's
        # per-scan bound re-grant re-seats the released field under
        # its recorded token — the unattended
        # `standby → promoting → active` walk under the `reclaim`
        # origin — the released field never stranding.
        release = foreign_io.request({"op": "release_writer"})
        evidence["release"] = release
        if claim_reclaim.unsupported_verb(release):
            raise Inconclusive(
                "release_writer answered invalid_request — the "
                "pinned release predates the claim lifecycle the "
                "leg stages",
                f"release_writer answered {release}",
            )
        if release.get("result") != "done":
            raise Abort(
                f"the foreign claim's hand-back was refused: "
                f"{release}"
            )
        reclaimed = None
        end_at = time.monotonic() + RESOLVE_BOUND
        watch = []
        while time.monotonic() < end_at:
            report = role(duty_url)
            if report is not None:
                kind = (
                    report.get("role")
                    + "/"
                    + stranded_rejoin.sync_kind(report)
                )
                if not watch or watch[-1] != kind:
                    watch.append(kind)
                if report.get("role") == "active":
                    reclaimed = report
                    break
                if report.get("role") not in ("standby", "promoting"):
                    failures.append(
                        "the fenced ex-owner reported "
                        f"{report} resolving the released field — "
                        "it may only walk back toward active"
                    )
                    raise Abort
            time.sleep(POLL_INTERVAL)
        evidence["reclaim_watch"] = watch
        if reclaimed is None:
            failures.append(
                "the released field never resolved — the fenced "
                "ex-owner never re-armed its recorded claim past "
                f"the claim's release; its reports stayed {watch}"
            )
            raise Abort
        journal = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        walk = stranded_rejoin.role_walk(journal[floor:])
        evidence["reclaim_walk"] = walk
        if ("standby", "promoting", "reclaim") not in walk or (
            "promoting",
            "active",
            "reclaim",
        ) not in walk:
            failures.append(
                "the released field's reclaim left no journaled "
                "walk back to active under the `reclaim` origin — "
                "an operator or restart path ran instead: "
                f"{walk}"
            )
            raise Abort
        verdict = verdict_io.request({"op": "step", "dt": 0})
        evidence["restored_verdict"] = verdict
        if not claim_reclaim.mutation_fenced(verdict):
            failures.append(
                "the re-seated claim does not fence third-party "
                f"mutations: {verdict}"
            )
            raise Abort
        if claim_reclaim.verdict_owner(verdict) != owner_token:
            failures.append(
                "the released claim did not re-arm to the "
                "recorded owner — the fencing verdict names "
                f"{claim_reclaim.verdict_owner(verdict)}: {verdict}"
            )
            raise Abort
        declared1 = stranded_rejoin.verdict_monitor(verdict)
        if declared1 is None:
            raise Inconclusive(
                "the re-armed claim declares no monitor — the "
                "pinned release predates the claim-declared "
                "monitor the contract carries",
                f"the fencing verdict was {verdict}",
            )
        kind1 = monitor_kind(declared1, monitor_addr(duty_url))
        if kind1 == "wildcard":
            raise Inconclusive(
                "the re-armed claim declares its wildcard bind "
                "verbatim — the pinned release predates the "
                "claim-monitor normalization",
                f"the declared monitor was {declared1}",
            )
        if kind1 != "owner":
            failures.append(
                "the re-armed claim declares monitor "
                f"{declared1} — not the ex-owner's dialable "
                f"monitor {monitor_addr(duty_url)}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "resolve",
                "field": "reclaimed",
                "owner": "recorded",
            }
        )

        # Phase 7 — the reconverge: the orphaned-tracking sibling
        # re-joins the re-seated owner, and the pair rests on its
        # launch roles — the manifest-declared duty `active`, its
        # standby `tracking`, the claim under the launch owner's
        # token.
        standby_role = wait_role(
            standby_url,
            claim_reclaim.tracking,
            RECONVERGE_BOUND,
            failures,
            "the orphaned sibling never reconverged onto the "
            "re-seated owner",
        )
        duty_role = role(duty_url)
        if duty_role is None or duty_role.get("role") != "active":
            failures.append(
                "the restored pair's duty reports "
                f"{duty_role} — expected active"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "restore",
                "duty": "active",
                "standby": stranded_rejoin.sync_kind(standby_role),
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
        # Detach the claim attachments cleanly: release whatever
        # hold each still carries — a hold left standing keeps the
        # field claimed for a dead token — then close. The release
        # drops only the connection's own hold, so it never takes
        # the owner's claim down with it.
        for client in (verdict_io, foreign_io):
            if client is not None:
                try:
                    client.request({"op": "release_writer"})
                except Exception:
                    pass
                client.close()
        if dead is not None:
            dead.close()
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
        choices=["expect-collapse"],
        help="doctor the leg's expectation to the defect's shape — "
        "the standing dead-monitor claim may collapse the orphaned "
        "peers' paced cadence, so the held window must report the "
        "named diagnostic",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = (
            orphan_probe_cadence_pass(args, args.tamper)
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "orphan-probe-cadence: the doctored expectation "
                "wanted the standing dead-monitor claim collapsing "
                "the orphaned peers' paced cadence — an inconclusive "
                "run offers the doctored case no evidence"
            )
            return 1
        # The digest line carries only the stable reason — the
        # run's own verdicts, endpoints, and measured counters
        # report on stderr, where two identical passes need not
        # share them.
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"orphan-probe-cadence: inconclusive — {detail}")
        print(
            f"orphan-probe-cadence-digest inconclusive — {reason}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"orphan-probe-cadence: {line}")
        return 1
    for failure in failures:
        eprint(f"orphan-probe-cadence: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"orphan-probe-cadence: the {args.tamper} case "
                "passed silently — the leg never noticed the "
                "doctored expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"orphan-probe-cadence-digest {digest} — the held "
        "dead-monitor claim bounded the orphaned peers' probe "
        "cost per window, the paced cadence holding and the "
        "overrun counters inside the bounded-probe slack, the "
        "released field reclaimed under the launch owner's "
        "token, and the pair back on its launch roles"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
