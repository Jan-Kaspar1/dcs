#!/usr/bin/env python3
"""The demote-follow reconvergence leg for the reference plant — the
consumer-side proof that a demoted field owner reconverges to tracking
on the tracking source its successor's checkpoint pulls announced,
under the manifest's declared wildcard listen binds (WW-ENG-003,
WW-LCM-001).

The pair leg (`ci/pair.py`) proves the declared pair switches and
reconverges; the peer-announce leg (`ci/peer_announce.py`) proves the
`?peer=` acceptance half. This leg pins the follow-peer half of the
tracking-source contract the #616/#618/#619/#620 defect fixes settle,
on the customer-declared deployment that reproduces the #619 defect
shape: both controllers bind the manifest's `0.0.0.0` listen host, so
a tracking peer's `GET /checkpoint?peer=` pulls announce a wildcard
address and the recorded source must resolve to the pull connection's
proven source — a dialable peer address, never the wildcard verbatim
(the undialable record the defect family left), never the demoted
peer's own address (the self-pin), never a foreign endpoint. The rig
is the pair legs' shared one — `pair.launch_pair` reads the standby
wiring and persistence fields out of `deploy/manifest.json` and, under
the leg's declared-binds launch, binds each controller's `--listen` on
its declared host with a runner-assigned port. The run:

- convergence — the driven-tick loop converges the declared standby
  to `tracking`, each of its pulls announcing its wildcard-bound
  monitor address on the field owner, the resolved source the owner's
  demotion fallback records;
- the forward switch — the documented `demote`/`promote`: the launched
  active's only tracking source is the recorded announce, so its
  demotion verifies and adopts it — the journaled
  `tracking_source_adopted` entry audited for the dialable resolved
  address — and the demoted peer reconverges `tracking`, holding it
  across a driven pull train while every served role report is
  audited for a wildcard, self-addressed, or foreign pull target;
- the fail-back switch — the same documented order reversed: the
  promoted peer demotes onto its configured tracking source and the
  reconverged peer promotes, the manifest's declared roles restored
  and the second demoted peer holding `tracking` across its own pull
  train;
- the record — each peer's served snapshots and adopted receipt logs
  identical throughout, each durable journal file carrying its own
  `role_changed` transitions in `seq` order under the single
  cold-start boundary — no restart boundary the switching sequence
  could have introduced — and each declared state file checkpointing
  the run's final tick under the manifest's model fingerprint.

Usage:

    demote_reconvergence.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `demote-reconvergence-digest <sha256>` line prints —
the check runs two passes and compares them
(`demote-reconvergence-nondeterministic`). A contract violation
reports `demote-reconvergence: …` lines on stderr and exits 1 — the
check's `demote-reconvergence-failed`. `--tamper self-announce`
crafts a `?peer=` announce naming the field owner's own monitor
address — a claim the pulling connection's own source proves, so it
lands exactly as a self-claim — planting a self-addressed demotion
hint: the demotion's verify pull reads the owner's own document, the
replayable own-document shape an announced demotion refuses, so
`POST /demote` answers `409 no_tracking_source` and the leg must
report the refusal rather than let a self-pinned demotion proceed.
"""

import argparse
import hashlib
import json
import os
import sys

import pair


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The consecutive driven pulls a reconverged peer must hold `tracking`
# across — each driven tick is a checkpoint pull, so a tracking source
# re-poisoning on every pull cannot survive the hold.
HOLD_TICKS = 5


def addr(url):
    """The peer's dialable monitor address as `host:port` — the form
    a correct tracking-source record names once the announced
    wildcard resolves to the connection's proven source."""
    return url.removeprefix("http://")


def fetch_source(detail):
    """The pull target a `degraded` detail names — the 'fetch from
    <addr>: <error>' string a failed checkpoint pull reports — or
    None when the detail names no source."""
    if not isinstance(detail, str) or "fetch from " not in detail:
        return None
    source = detail.split("fetch from ", 1)[1].split(": ", 1)[0]
    return source or None


def source_kind(source, own_addr, peer_addr):
    """Classify a recorded tracking source: 'wildcard' for an
    unspecified bind address — the announced `--listen 0.0.0.0` the
    defect family recorded verbatim — 'self' for the demoted peer's
    own monitor address, 'peer' for the successor's, 'foreign' for
    anything else. Only 'peer' is a dialable contract answer."""
    if "0.0.0.0" in source or "[::]" in source:
        return "wildcard"
    if source == own_addr:
        return "self"
    if source == peer_addr:
        return "peer"
    return "foreign"


def tracking_report(report):
    """Whether a served RoleReport is a tracking standby — the
    promotable posture a documented switch needs."""
    return (
        isinstance(report, dict)
        and report.get("role") == "standby"
        and "tracking" in (report.get("sync") or {})
    )


def adopted_sources(path):
    """The verbatim `source` addresses a journal file's
    `tracking_source_adopted` entries record — the adoption audit the
    digest's port normalization must not hide."""
    sources = []
    with open(path) as handle:
        for line in handle:
            record = json.loads(line)
            adopted = (
                record.get("entry", {})
                .get("event", {})
                .get("tracking_source_adopted")
            )
            if adopted is not None:
                sources.append(adopted["source"])
    return sources


def audit_report(name, report, own_addr, peer_addr, failures):
    """Audit one served RoleReport: a wildcard inside the sync state
    is an announced bind address recorded as a tracking source, and a
    `degraded` detail's named pull target must be the successor's
    dialable monitor address."""
    sync = report.get("sync")
    text = json.dumps(sync)
    if "0.0.0.0" in text or "[::]" in text:
        failures.append(
            f"{name} reports a wildcard tracking source: {text[:250]} "
            "— an announced 0.0.0.0 bind is undialable"
        )
    detail = (
        sync.get("degraded", {}).get("detail")
        if isinstance(sync, dict)
        else None
    )
    source = fetch_source(detail)
    if source is not None:
        kind = source_kind(source, own_addr, peer_addr)
        if kind != "peer":
            failures.append(
                f"{name} tracks a {kind} source {source} — the "
                "recorded tracking source must be the successor's "
                "dialable monitor address"
            )


def audit_adoption(name, journal_path, own_addr, peer_addr, failures,
                   expect):
    """Audit a peer's journaled `tracking_source_adopted` entries:
    each adopted source must be the successor's dialable monitor
    address — never the wildcard bind the announce carried, never the
    demoted peer's own address, never a foreign endpoint. `expect`
    requires at least one entry — an announced-only demotion must
    journal its verified adoption. Returns the recorded sources."""
    sources = []
    if journal_path is None:
        if expect:
            failures.append(
                f"{name} declares no journal file — the adoption "
                "audit has no durable record"
            )
    elif os.path.exists(journal_path):
        sources = adopted_sources(journal_path)
    if expect and not sources:
        failures.append(
            f"{name}'s announced-source demotion journaled no "
            "tracking_source_adopted — the adoption went unrecorded"
        )
    for source in sources:
        kind = source_kind(source, own_addr, peer_addr)
        if kind != "peer":
            failures.append(
                f"{name}'s demotion adopted a {kind} tracking source "
                f"{source} — the demoted peer must track its "
                "successor's dialable monitor address"
            )
    return sources


def reconvergence_pass(args, tamper):
    """The demote/reconverge/fail-back run on the manifest-declared
    pair under its declared wildcard binds. Returns
    `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "demote-reconvergence leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared, declared_binds=True)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_addr, standby_addr = addr(duty_url), addr(standby_url)
        duty_name, standby_name = duty_decl["name"], standby_decl["name"]

        # The declared wildcard binds deployed verbatim — the #619
        # defect shape: each peer's checkpoint pulls announce the
        # wildcard address its --listen bound, so the tracking source
        # a demoted peer records must resolve to the connection's
        # proven source.
        for name, entry, bound in (
            (duty_name, duty_decl, rig.duty_bound),
            (standby_name, standby_decl, rig.standby_bound),
        ):
            declared_host = entry.get("listen", "").rsplit(":", 1)[0]
            bound_host = bound[0].rsplit(":", 1)[0] if bound else None
            if bound_host != declared_host:
                failures.append(
                    f"{name} did not bind its declared listen host "
                    f"{declared_host} — the spawn reported "
                    f"{bound or 'no listener'}"
                )
        if failures:
            raise Abort

        # Each peer's only legitimate tracking source is the other's
        # dialable monitor address, so one audit mapping serves both
        # switch directions.
        def audit_peer(name, url, own_addr, peer_addr):
            report = pair.get(f"{url}/role", "GET /role", failures)
            audit_report(name, report, own_addr, peer_addr, failures)
            return report

        def watch():
            audit_peer(duty_name, duty_url, duty_addr, standby_addr)
            audit_peer(standby_name, standby_url, standby_addr, duty_addr)
            if failures:
                raise Abort

        # Phase 1 — convergence: the driven-tick loop, the tracking
        # peer scanned first so each pull applies the owner's latest
        # checkpoint. Every pull announces the standby's wildcard-bound
        # monitor address on the field owner — the genuine announce
        # the demotion's tracking-source fallback resolves.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # The doctored case: a crafted ?peer= announce naming the
        # field owner's own monitor address — the pulling connection's
        # own source, so it lands exactly as a self-claim — plants a
        # self-addressed demotion hint: the self-pin defect shape this
        # leg exists to catch. The hint's verify pull reads the
        # owner's own document — the replayable own-document shape the
        # demotion refuses — so the switch's POST /demote must answer
        # 409 no_tracking_source rather than adopt the self-pin. The
        # checkpoint read still answers the owner's checkpoint.
        if tamper == "self-announce":
            checkpoint = pair.get(
                f"{duty_url}/checkpoint?peer={duty_addr}",
                "GET /checkpoint?peer=<self>",
                failures,
            )
            if (
                not isinstance(checkpoint, dict)
                or checkpoint.get("tick") != converged["owner"]["tick"]
                or checkpoint.get("model_fingerprint") != rig.fingerprint
            ):
                failures.append(
                    "the crafted self-announce's checkpoint read "
                    f"answered {checkpoint}, expected the field "
                    f"owner's checkpoint at tick "
                    f"{converged['owner']['tick']} under fingerprint "
                    f"{rig.fingerprint}"
                )
                raise Abort

        # Phase 2 — the forward switch: demote the launched active —
        # its only tracking source the recorded announce — and promote
        # the converged standby. The demotion's verified adoption is
        # journaled before the demoted peer's first pull, so the
        # adopted-source audit runs at the decisive point; every
        # served report across the handover is audited for a wildcard,
        # self-addressed, or foreign pull target.
        adopted = {}

        def adoption():
            sources = audit_adoption(
                duty_name,
                rig.duty_files.get("journal_file"),
                duty_addr,
                standby_addr,
                failures,
                expect=True,
            )
            adopted["kinds"] = [
                source_kind(source, duty_addr, standby_addr)
                for source in sources
            ]
            if failures:
                raise Abort

        switched = rig.switch(
            duty_url,
            standby_url,
            failures,
            audit_receipts=True,
            after_promote=adoption,
            after_tick=watch,
        )
        evidence["reconverged"] = switched["ticks"][-1]
        digest_entries.append(
            {
                "phase": "forward",
                "demote": switched["demote"],
                "promote": switched["promote"],
                "ticks": switched["ticks"],
                "demoted_role": switched["demoted_role"],
                "promoted_role": switched["promoted_role"],
                "receipts": switched["receipts"],
                "adopted": adopted["kinds"],
            }
        )

        # Phase 3 — the pull train: driven ticks scanning the demoted
        # peer first — each a checkpoint pull — the reconverged peer
        # holding `tracking` throughout, its served report audited
        # each pull.
        held = []
        for _ in range(HOLD_TICKS):
            _tracked, owner = rig.tick(duty_url, standby_url, failures)
            report = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            audit_report(
                duty_name, report, duty_addr, standby_addr, failures
            )
            held.append(report)
        if failures:
            raise Abort
        if not all(tracking_report(report) for report in held):
            failures.append(
                "the reconverged peer dropped out of tracking across "
                "repeated pulls: "
                + json.dumps([r.get("sync") for r in held])[:400]
            )
            raise Abort
        evidence["hold"] = len(held)
        digest_entries.append({"phase": "hold", "reports": held})

        # Phase 4 — the fail-back: the same documented order reversed.
        # The promoted peer demotes onto its configured tracking
        # source — the declared --standby wiring — and the reconverged
        # peer promotes, restoring the manifest's declared roles with
        # the same per-tick source audit.
        restored = rig.switch(
            standby_url,
            duty_url,
            failures,
            demote_what="the promoted peer",
            promote_what="the reconverged peer",
            audit_receipts=True,
            after_tick=watch,
        )
        evidence["restored"] = restored["ticks"][-1]
        digest_entries.append(
            {
                "phase": "restore",
                "demote": restored["demote"],
                "promote": restored["promote"],
                "ticks": restored["ticks"],
                "demoted_role": restored["demoted_role"],
                "promoted_role": restored["promoted_role"],
                "receipts": restored["receipts"],
            }
        )

        # The restore's own pull train — the second demoted peer
        # holding `tracking` the same way.
        held = []
        for _ in range(HOLD_TICKS):
            _tracked, owner = rig.tick(standby_url, duty_url, failures)
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            audit_report(
                standby_name, report, standby_addr, duty_addr, failures
            )
            held.append(report)
        if failures:
            raise Abort
        if not all(tracking_report(report) for report in held):
            failures.append(
                "the restored standby dropped out of tracking across "
                "repeated pulls: "
                + json.dumps([r.get("sync") for r in held])[:400]
            )
            raise Abort
        digest_entries.append({"phase": "hold-restored", "reports": held})
        final_tick = owner["tick"]

        # Phase 5 — the settled record: the launch roles restored with
        # no residual degraded marker, the adopted receipt logs one
        # identical log.
        final_duty = pair.get(f"{duty_url}/role", "GET /role", failures)
        final_standby = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if final_duty.get("role") != "active" or not tracking_report(
            final_standby
        ):
            failures.append(
                "the launch roles did not restore — "
                f"{duty_name} answers {final_duty}, "
                f"{standby_name} answers {final_standby}"
            )
        for name, report in (
            (duty_name, final_duty),
            (standby_name, final_standby),
        ):
            if isinstance(report.get("sync"), dict) and "degraded" in (
                report["sync"]
            ):
                failures.append(
                    f"{name} still reports a degraded marker after "
                    f"restore: {json.dumps(report['sync'])[:250]}"
                )
        receipts_duty = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        receipts_standby = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts_standby:
            failures.append(
                "the peers' receipt logs diverged across the run — "
                "the adopted audit is not one log"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "final",
                "duty_role": final_duty,
                "standby_role": final_standby,
                "receipts": receipts_duty,
            }
        )

        # Phase 6 — the durable record: each peer's declared journal
        # file carrying its own role transitions under the single
        # cold-start boundary with `seq` order intact — no restart
        # boundary the switching sequence could have introduced —
        # every `tracking_source_adopted` naming a dialable peer
        # source, and each declared state file checkpointing the run's
        # final tick under the manifest's fingerprint.
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
        adoptions = {
            duty_name: (duty_addr, standby_addr, True),
            standby_name: (standby_addr, duty_addr, False),
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
                    record_ for kind, record_ in records if kind == "boundary"
                ]
                if boundaries != [{"run": 1, "tick": 0}]:
                    failures.append(
                        f"{name}'s journal boundaries are {boundaries}, "
                        "expected the single cold-start marker — a "
                        "restart boundary inside the switch sequence "
                        "is a process restart"
                    )
                entries = [
                    record_ for kind, record_ in records if kind == "entry"
                ]
                seqs = [entry["seq"] for entry in entries]
                if seqs != list(range(1, len(seqs) + 1)):
                    failures.append(
                        f"{name}'s journal seqs are not 1..n in order: "
                        f"{seqs}"
                    )
                transitions = pair.role_transitions(entries)
                want = expected[name]
                if [(frm, to) for _tick, frm, to in transitions] != want:
                    failures.append(
                        f"{name}'s journal file carries the role "
                        f"transitions {transitions}, expected {want}"
                    )
                own_addr, peer_addr, expect = adoptions[name]
                sources = audit_adoption(
                    name,
                    journal_path,
                    own_addr,
                    peer_addr,
                    failures,
                    expect=expect,
                )
                if name == duty_name and len(sources) != 1:
                    failures.append(
                        f"{name}'s journal carries {len(sources)} "
                        "tracking_source_adopted entries, expected "
                        "exactly the announced-source demotion's one"
                    )
                record["journal_records"] = records
                record["adopted"] = [
                    source_kind(source, own_addr, peer_addr)
                    for source in sources
                ]
            if state_path is not None:
                if not os.path.exists(state_path):
                    failures.append(
                        f"{name}'s declared state file {state_path} "
                        "does not exist — the --state-file flag was "
                        "not honored"
                    )
                else:
                    try:
                        with open(state_path) as handle:
                            checkpoint = json.load(handle)
                    except (OSError, json.JSONDecodeError) as error:
                        failures.append(
                            f"{name}'s state file does not parse: {error}"
                        )
                        checkpoint = None
                    if checkpoint is not None:
                        if checkpoint.get("tick") != final_tick:
                            failures.append(
                                f"{name}'s state file persisted tick "
                                f"{checkpoint.get('tick')} while the run "
                                f"stood at {final_tick}"
                            )
                        if (
                            checkpoint.get("model_fingerprint")
                            != rig.fingerprint
                        ):
                            failures.append(
                                f"{name}'s state file carries fingerprint "
                                f"{checkpoint.get('model_fingerprint')}, the "
                                f"manifest declares {rig.fingerprint}"
                            )
                        record["state_tick"] = checkpoint.get("tick")
            persisted[name] = record
        if failures:
            raise Abort
        digest_entries.append({"phase": "record", "persisted": persisted})
        evidence["final_tick"] = final_tick
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
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
        choices=["self-announce"],
        help="craft a ?peer= announce naming the field owner's own "
        "monitor address — landing on the pulling connection's own "
        "source — so the demotion faces a self-addressed tracking "
        "hint the leg must refuse as no_tracking_source",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = reconvergence_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"demote-reconvergence: {line}")
        return 1
    for failure in failures:
        eprint(f"demote-reconvergence: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"demote-reconvergence: the {args.tamper} case passed "
                "silently — the leg never noticed the self-addressed "
                "demotion hint"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"demote-reconvergence-digest {digest} — tracking by tick "
        f"{evidence['converged']}, demoted peer tracking its announced "
        f"successor at tick {evidence['reconverged']}, tracking held "
        f"across {evidence['hold']} pulls, launch roles restored at "
        f"tick {evidence['restored']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
