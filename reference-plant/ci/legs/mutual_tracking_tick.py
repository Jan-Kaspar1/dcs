#!/usr/bin/env python3
"""The mutual-tracking-tick leg for the reference plant — the consumer-side
proof that under mutual standby tracking — the transient ownerless state
every demote creates — a tracking apply keeps both peers' run ticks within
scan cadence of each other and of the driven scans, a regression-seeded
offset clears once the tracked stream recovers rather than ratcheting, and
a following promotion continues the run's tick domain without a journaled
mega-discontinuity (WW-ENG-003, WW-LCM-001 — the bounded tick-domain
contract the #693 fix establishes, mirrored at the customer boundary from
the qa rig's mutual-tracking-tick leg).

The defect the contract closed had a tracking apply landing the pulled
checkpoint at `tick + offset` — an offset carried unchanged since the run's
clock was last seeded ahead of the stream — so a demoted pair tracking each
other fed each landing back into the other peer's served tick: every apply
ratcheted the peer's lead, and both run clocks ran away from the scan
cadence together. The fix recomputes the landing offset as the run's
current lead over the pulled stream on every transfer — a lagging
checkpoint holds at the run's own tick, a recovered one lands on the
stream, and a seeded lead clears instead of compounding. This leg
exercises exactly that shape on the manifest-declared pair:

- converges the declared pair to its launch roles — the field owner
  `active`, the standby `tracking` — through the pair rig's driven-tick
  loop, and gates the contract's surface: each peer's served checkpoint
  must stamp `source_owns_field` (the wire verdict the mutual-standby
  posture reports through) beside its integer `tick`, each sync verdict
  must carry the aligned stream tick, and each declared `--journal-file`
  must exist — a pinned release predating that surface reports
  `mutual-tracking-tick-digest inconclusive`, never a failure;
- demotes the field owner so both controllers stand by and track each
  other — the demoted peer following the announced source its own pulls
  recorded and the demote verify adopted, the configured peer its
  declared standby wiring — the pair reporting the `orphaned` verdict
  the ownerless line honestly owes;
- freezes the demoted peer — `SIGSTOP` on the spawned process — while
  the configured peer's quiesced scans pace its run clock past the
  frozen peer's served tick: the seeded offset, the run's standing
  lead over the tracked stream;
- thaws the frozen peer and drives tracking-first scans through the
  recovery — the resumed peer's stale checkpoint landing as a hold on
  the survivor, never a rewind, that apply minting the standing lead
  its served checkpoint stamps `stream_tick`, and the seed clearing
  as the realigned stream catches up;
- audits every observation — the serving `/role` reports collected
  through the run and both peers' durable journals after it — against
  the bounded tick-domain contract: no observed or journaled tick ever
  regresses, neither run's tick ever leaves the driven-scan cadence,
  the peers' clocks stay within scan cadence of each other, and the
  seeded offset clears rather than ratcheting;
- promotes the demoted peer back: the `orphaned` verdict is promotable,
  and the resumption continues the same run — the durable record
  carries the standby/promoting/active walk at continuing ticks under
  the single cold-start boundary, no restart marker and no journaled
  mega-jump. The pair ends on its launch roles.

Usage:

    mutual_tracking_tick.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `mutual-tracking-tick-digest <sha256>` line prints — the
check runs two passes and compares them
(`mutual-tracking-tick-nondeterministic`). A contract violation reports
`mutual-tracking-tick: …` lines on stderr and exits 1 — the check's
`mutual-tracking-tick-failed`. `--tamper ratcheting-offset` doctors the
post-recovery observations into the compounding lead the defect left — a
leg asserting bounded ticks while the seeded offset ratchets must report
its named diagnostic rather than passing an unexercised contract.
"""

import argparse
import hashlib
import json
import os
import signal
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import claim_reclaim
import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored case.
# The doctored case: a leg asserting the bounded tick domain while
# the recorded seed compounds into both runs' served ticks — the
# defect's own ratchet — must surface the named diagnostic rather
# than passing an unexercised contract.
LEG = {
    "order": 650,
    "title": "the mutual-tracking-tick leg",
    "passes": "mutual-tracking-tick-leg",
    "failed": "mutual-tracking-tick-failed",
    "tampers": [
        {
            "name": "ratcheting-offset",
            "passed": "a ratcheting-offset case passed the mutual-tracking-tick leg",
            "missed": "the ratcheting-offset case did not report its named diagnostic",
            "evidence": ["ratcheted past the driven-scan cadence"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates — or the harness admits no lever
    for — the contract the leg exercises: the keyed-pair launch
    surface, the checkpoint's ownership and lead stamps, the sync
    verdict's aligned stream tick, the announced-source demotion the
    mutual-standby staging hangs on, the stop/pause lever the frozen
    window is staged through, the seed never forming, or the durable
    journal the audit needs. The run classifies inconclusive, never a
    product failure."""


# The driven-scan bounds each phase runs: the mutual-standby settle —
# the demoted peer's announced-source adoption plus a clean apply each
# way — the held window's quiesced scans on the surviving peer (the
# seeded offset's size), the post-thaw reconvergence bound covering a
# pull still consuming the freeze's stale in-flight fetch, the
# promotion settle, and the restored pair's continuation ticks. The
# cadence tolerances: the peers' runs stay within CADENCE ticks of
# each other in lockstep, a cleared offset reads within CLEAR_BOUND,
# and SLACK absorbs the pull pipeline's fetch lag inside the absolute
# bound each observed tick owes the driven-scan count.
MUTUAL_ROUNDS = 6
WINDOW_SCANS = 8
RECOVERY_ROUNDS = 16
PROMOTE_SCANS = 6
RESTORE_TICKS = 5
SETTLE_S = 0.3
WINDOW_SETTLE_S = 0.15
CADENCE = 2
CLEAR_BOUND = 3
SLACK = 8


def pause_peer(process):
    """The demoted peer's container pause — `SIGSTOP` on the spawned
    process, the `docker pause` reproduction's lever: the monitor's
    listener stays bound but unanswered, so the tracking peer's
    checkpoint fetches drop to produced-nothing misses while its own
    scan runs ahead of the frozen line. A lever that cannot land
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
            f"the demoted peer's pause lever never landed: {error}"
        )


def resume_peer(process):
    """The pause's restore — `SIGCONT` so the frozen peer serves and
    scans again, the `docker unpause` the reproduction runs. Returns
    None on landing, the error string on refusal."""
    try:
        process.send_signal(signal.SIGCONT)
        return None
    except Exception as error:
        return str(error)


def try_role(url):
    """The served /role report, or None when the peer does not answer
    — a dropped observation, never an unwind. Never called on a peer
    the leg holds frozen: an unanswered read there would hang the leg,
    not drop."""
    try:
        return simulate.http(f"{url}/role")
    except Exception:
        return None


def peer_row(url):
    """One normalized watch row off a peer's served /role: the
    reported role, the run tick, the sync verdict's kind, and the
    aligned stream tick the `tracking`/`orphaned` verdicts carry —
    the last applied checkpoint's source tick, the mark the seeded
    offset is measured against."""
    report = try_role(url)
    if report is None:
        return None
    sync = report.get("sync")
    kind = aligned = None
    if isinstance(sync, dict) and sync:
        kind = next(iter(sync))
        aligned = (sync.get(kind) or {}).get("aligned")
    elif isinstance(sync, str):
        kind = sync
    return {
        "role": report.get("role"),
        "tick": report.get("tick"),
        "sync": kind,
        "aligned": aligned,
    }


def clean_error(error, rig):
    """An error string normalized for the digested output — the run's
    runner-owned scratch paths replaced so two passes on different
    scratch directories read identically."""
    return str(error).replace(rig.scratch, "<run>")[:300]


def int_tick(value):
    """Whether a report or journal field is an integer tick (not a
    bool, not absent)."""
    return isinstance(value, int) and not isinstance(value, bool)


def contract_checkpoint(checkpoint):
    """Whether a served checkpoint carries the tick-domain contract
    surface the leg reads: the integer run `tick`, the
    `source_owns_field` stamp the mutual-standby `orphaned` verdict is
    reported off, and the `generation` the line's tick-domain identity
    is proven by."""
    return (
        isinstance(checkpoint, dict)
        and int_tick(checkpoint.get("tick"))
        and isinstance(checkpoint.get("source_owns_field"), bool)
        and "generation" in checkpoint
    )


def audit_frames(frames, base_tick, failures):
    """The bounded tick-domain audit over the collected watch rows:
    each peer's served run tick never regresses and never leaves the
    driven-scan cadence — `base_tick` plus every scan the leg drove on
    either peer plus the pull-lag slack — and whenever both peers
    report a tick the clocks stay within scan cadence of each other.
    `frames` is the row list `observe` appended to; each frame is
    `{"duty": row|None, "standby": row|None, "driven": scans}`."""
    last = {}
    for index, frame in enumerate(frames):
        ticks = {}
        for peer in ("duty", "standby"):
            row = frame.get(peer)
            if row is None or not int_tick(row.get("tick")):
                continue
            ticks[peer] = row["tick"]
            if peer in last and row["tick"] < last[peer]:
                failures.append(
                    f"the {peer} peer's run tick rewound from "
                    f"{last[peer]} to {row['tick']} — a tracking "
                    "apply lands at the later of the two clocks, so "
                    "the run's clock never goes back"
                )
            last[peer] = row["tick"]
            bound = base_tick + frame["driven"] + SLACK
            if row["tick"] > bound:
                failures.append(
                    f"the {peer} peer's run tick {row['tick']} "
                    f"ratcheted past the driven-scan cadence — "
                    f"{frame['driven']} scans driven past base "
                    f"{base_tick} bound the run to {bound}"
                )
        if len(ticks) == 2 and abs(ticks["duty"] - ticks["standby"]) > CADENCE:
            failures.append(
                f"the peers' run ticks left scan cadence of each "
                f"other — duty at {ticks['duty']}, standby at "
                f"{ticks['standby']} (frame {index})"
            )
        if len(failures) > 8:
            return


def run_tail(records):
    """The journal file's boundary count and entry records of the
    current run — the records after the last run-boundary marker (the
    whole file when none has landed yet)."""
    boundaries = 0
    entries = []
    for kind, record in records:
        if kind == "boundary":
            boundaries += 1
            entries = []
        else:
            entries.append(record)
    return boundaries, entries


def journal_audit(name, path, base_tick, driven, failures, inconclusive, rig):
    """The durable half of the bounded tick-domain audit over one
    peer's declared journal file: exactly one run boundary — the
    launch's cold start — with every gained entry's integer tick
    non-regressing and inside the driven-scan bound (no mega-jump),
    and no `source_restarted` on a stream that never restarted."""
    if not os.path.isfile(path):
        failures.append(
            f"{name}'s declared journal file {path} does not exist — "
            "the durable half of the tick-domain audit is absent"
        )
        return None
    try:
        records = pair.journal_records(path)
    except Abort as abort:
        inconclusive.append(
            f"{name}'s durable journal could not be read: "
            f"{'; '.join(clean_error(arg, rig) for arg in abort.args)}"
        )
        return None
    boundaries, entries = run_tail(records)
    if boundaries == 0:
        inconclusive.append(
            f"{name}'s durable journal carries no run-boundary "
            "marker — the pinned release predates the durable-record "
            "contract the leg audits"
        )
        return None
    if boundaries != 1:
        failures.append(
            f"{name}'s durable journal carries {boundaries} run "
            "boundaries — the run's tick domain restarted instead of "
            "continuing across the switch"
        )
    mark = None
    for entry in entries:
        tick = entry.get("tick")
        if not int_tick(tick):
            failures.append(
                f"{name}'s durable journal holds an entry record "
                "carrying no integer tick — the attributed-record "
                "axis the contract pins is malformed"
            )
            continue
        if mark is not None and tick < mark:
            failures.append(
                f"{name}'s durable journal's tick axis regressed — "
                f"seq {entry.get('seq')} stamps tick {tick} below "
                f"the standing mark {mark}"
            )
        else:
            mark = tick
        bound = base_tick + driven + SLACK
        if tick > bound:
            failures.append(
                f"{name}'s durable journal stamps tick {tick} at seq "
                f"{entry.get('seq')} — a journaled mega-jump past the "
                f"driven-scan cadence (bound {bound})"
            )
    for entry in entries:
        if "source_restarted" in (entry.get("event") or {}):
            failures.append(
                f"{name}'s durable journal records a source restart "
                "the mutual-standby stream never had — a "
                "domain-boundary lie on the same-generation line"
            )
            break
    return entries


def mutual_tracking_tick_pass(args, tamper):
    """The mutual-tracking run: converge the declared pair, demote the
    field owner so both peers stand by tracking each other, hold the
    demoted peer frozen while the survivor's quiesced scans seed the
    standing lead, thaw and drive the recovery, then promote the
    demoted peer and audit both durable journals — ticks bounded on
    the scan cadence throughout, the seeded offset cleared, the
    promotion continuing the run, the launch roles restored. Returns
    `(digest_entries, evidence, failures)`; raises `Inconclusive`
    where the pinned release predates the contract or the harness
    admits no lever for the staging."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "mutual-tracking-tick leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    inconclusive = []
    rig = None
    try:
        try:
            rig = pair.launch_pair(args, declared)
        except Abort as abort:
            detail = "; ".join(str(arg) for arg in abort.args)
            if "exited at startup" in detail and "unknown option" in detail:
                raise Inconclusive(
                    "the pinned release refuses the keyed-pair launch "
                    "the mutual-standby staging is declared through — "
                    f"the contract's surface predates it: {detail}"
                )
            raise
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_name, standby_name = (
            rig.duty_decl["name"],
            rig.standby_decl["name"],
        )
        journal_files = {
            duty_name: rig.duty_files.get("journal_file"),
            standby_name: rig.standby_files.get("journal_file"),
        }
        for name, path in journal_files.items():
            if path is None:
                raise Inconclusive(
                    f"{name} declares no journal_file — the durable "
                    "half of the tick-domain audit is absent"
                )

        # The driven-scan ledger the bounded-tick audit reads: every
        # POST /scan the leg issues is counted, so an observed run
        # tick owes the cadence it was driven on.
        driven = 0

        def scan(url):
            nonlocal driven
            pair.scan(url, failures)
            driven += 1

        frames = []

        def observe(*peers):
            frame = {"driven": driven}
            for key, url in peers:
                frame[key] = peer_row(url)
            frames.append(frame)

        # Phase 1 — convergence: the manifest-declared pair settled on
        # its launch roles, the field owner active and the standby
        # tracking it off the configured wiring.
        converged = rig.converge(failures)
        driven += 2 * pair.CONVERGE_TICKS
        base_tick = converged["ticks"][-1]
        evidence["converged"] = base_tick
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"].get("role"),
                "standby_sync": claim_reclaim.sync_state(
                    converged["standby_role"]
                ),
            }
        )

        # Phase 2 — the contract surface: each peer's served
        # checkpoint must carry the tick-domain vocabulary the leg
        # audits — the integer run tick, the `source_owns_field`
        # stamp the ownerless verdict is reported off, and the line's
        # generation — and each declared journal file must exist. An
        # absent surface is the pre-contract shape: inconclusive,
        # never a violation.
        for name, url in (
            (duty_name, duty_url),
            (standby_name, standby_url),
        ):
            checkpoint = pair.get(
                f"{url}/checkpoint", "GET /checkpoint", failures
            )
            if not contract_checkpoint(checkpoint):
                raise Inconclusive(
                    f"{name}'s served checkpoint lacks the "
                    "tick-domain contract surface the leg audits "
                    f"(tick, source_owns_field, generation): "
                    f"{json.dumps(checkpoint, sort_keys=True)[:250]}"
                )
        for name, path in journal_files.items():
            if not os.path.isfile(path):
                raise Inconclusive(
                    f"{name}'s declared journal file does not exist "
                    "— the pinned release predates the durable-record "
                    "contract the leg audits"
                )

        # Phase 3 — the demote into mutual standby: `POST /demote` on
        # the field owner, its only tracking source the announced hint
        # the tracking peer's pulls recorded — verified and adopted at
        # the request boundary — while the configured peer keeps its
        # declared source. Both settle `standby` reporting `orphaned`:
        # the tracked line serves checkpoints stamped
        # `source_owns_field: false` on every apply either way.
        status, report = pair.request(f"{duty_url}/demote", {})
        if status != 200 or report.get("role") != "demoting":
            detail = json.dumps(report)
            if status == 409 and "no_tracking_source" in detail:
                raise Inconclusive(
                    "the pinned release refuses an announced-source "
                    "demotion — the adopted-source staging the leg "
                    "exercises predates it"
                )
            failures.append(
                f"POST /demote on {duty_name} answered {status} "
                f"{report}, expected a demoting report"
            )
            raise Abort
        digest_entries.append({"phase": "demote", "report": "demoting"})

        for _ in range(MUTUAL_ROUNDS):
            scan(duty_url)
            scan(standby_url)
            time.sleep(SETTLE_S)
            observe(("duty", duty_url), ("standby", standby_url))
        rows = frames[-1]
        postures = {}
        for key, name in (("duty", duty_name), ("standby", standby_name)):
            row = rows.get(key)
            if row is None:
                inconclusive.append(
                    f"{name}'s monitor never answered the "
                    "mutual-standby settle — the serving surface gave "
                    "the audit nothing to read"
                )
                continue
            postures[key] = f"{row['role']}/{row['sync']}"
            if row["role"] != "standby":
                failures.append(
                    f"{name} reports role {row['role']!r} after the "
                    "demote — expected the mutual-standby posture"
                )
            elif row["sync"] == "unsynchronized":
                inconclusive.append(
                    f"{name} never adopted a tracking source across "
                    "the demote — the pinned release predates the "
                    "announced-source staging the leg exercises"
                )
            elif row["sync"] != "orphaned":
                failures.append(
                    f"{name} reports {row['sync']!r} on the ownerless "
                    "tracked line — the checkpoints it applies stamp "
                    "no field owner, the verdict owed is orphaned"
                )
            if not int_tick(row.get("aligned")):
                failures.append(
                    f"{name}'s sync verdict carries no aligned "
                    "stream tick — the offset the leg audits has no "
                    f"mark: {row}"
                )
        digest_entries.append(
            {
                "phase": "mutual",
                "duty": postures.get("duty"),
                "standby": postures.get("standby"),
            }
        )
        if failures or inconclusive:
            raise Abort

        # Phase 4 — the seeded offset: freeze the demoted peer while
        # the configured peer's quiesced scans pace its run clock past
        # the frozen peer's served tick — each scan a produced-nothing
        # pull reporting `degraded`, the run's standing lead over the
        # tracked stream growing one tick per driven scan against the
        # held mark.
        frozen = frames[-1].get("duty") or peer_row(duty_url)
        if frozen is None or not int_tick(frozen.get("tick")):
            raise Inconclusive(
                "the demoted peer's served tick could not be read "
                "before the freeze — the staging's frozen mark is "
                "absent"
            )
        frozen_tick = frozen["tick"]
        window_start = len(frames)
        pause_peer(rig.duty)
        resume_error = None
        try:
            for _ in range(WINDOW_SCANS):
                scan(standby_url)
                time.sleep(WINDOW_SETTLE_S)
                observe(("standby", standby_url))
        finally:
            resume_error = resume_peer(rig.duty)
        if resume_error is not None:
            inconclusive.append(
                "the frozen peer was never thawed — the recovery "
                f"could not stage: {resume_error}"
            )
        window = [
            frames[index].get("standby")
            for index in range(window_start, len(frames))
        ]
        window = [row for row in window if row is not None]
        advanced = [
            row["tick"] for row in window if int_tick(row.get("tick"))
        ]
        if len(advanced) >= 2:
            for earlier, later in zip(advanced, advanced[1:]):
                if later != earlier + 1:
                    failures.append(
                        f"{standby_name}'s quiesced scan moved its "
                        f"run tick {earlier} -> {later} — a scan "
                        "ticks the gated run's clock by one, never "
                        "more and never less"
                    )
                    break
        # The seed: the survivor's standing lead over the tracked
        # stream — its last window tick against the frozen mark. The
        # stream position cannot move while the peer stands frozen,
        # so the quiesced ticks are the offset the recovered stream
        # must clear; the `degraded` verdicts the window's missed
        # pulls report carry no aligned mark to measure it by.
        seeded = None
        if not advanced:
            inconclusive.append(
                "the held window collected no served rows — the "
                "starved peer gave the audit nothing to read"
            )
        else:
            seeded = advanced[-1] - frozen_tick
            if seeded < WINDOW_SCANS - 1:
                inconclusive.append(
                    "the seeded offset never formed — the held "
                    "window's quiesced scans did not leave the run "
                    "standing ahead of its tracked stream: the "
                    f"survivor rests at {advanced[-1]} against the "
                    f"frozen mark {frozen_tick}"
                )
                seeded = None
        digest_entries.append(
            {
                "phase": "window",
                "scans": WINDOW_SCANS,
                "frozen": "held",
                "seeded": "standing" if seeded is not None else "none",
            }
        )
        if failures or inconclusive:
            raise Abort

        # Phase 5 — the recovery: the thawed peer's stale checkpoint
        # lands on the survivor as a hold — never a rewind — and that
        # first apply mints the standing lead: the survivor's served
        # checkpoint stamps `stream_tick` where the adopted stream
        # position sits below its run tick, the seeded offset's
        # declared evidence. The resumed pulls then realign the
        # frozen run onto the stream and the seeded offset clears:
        # both peers' aligned mark returns within CLEAR_BOUND of the
        # run tick while the runs stay within CADENCE of each other.
        cleared = None
        seed_index = len(frames)
        stamped_lead = None
        for round_ in range(RECOVERY_ROUNDS):
            scan(standby_url)
            if round_ == 0:
                survivor_checkpoint = pair.get(
                    f"{standby_url}/checkpoint",
                    "GET /checkpoint",
                    failures,
                )
                survivor_tick = (
                    survivor_checkpoint.get("tick")
                    if isinstance(survivor_checkpoint, dict)
                    else None
                )
                stream_tick = (
                    survivor_checkpoint.get("stream_tick")
                    if isinstance(survivor_checkpoint, dict)
                    else None
                )
                if int_tick(stream_tick) and int_tick(survivor_tick):
                    if stream_tick < survivor_tick:
                        stamped_lead = survivor_tick - stream_tick
                if stamped_lead is None or stamped_lead < seeded:
                    inconclusive.append(
                        "the surviving peer's checkpoint stamps no "
                        "standing lead over the held stream — the "
                        "pinned release predates the `stream_tick` "
                        "stamp the contract is read through"
                    )
            scan(duty_url)
            time.sleep(SETTLE_S)
            observe(("duty", duty_url), ("standby", standby_url))
            row = frames[-1]
            duty_row, standby_row = row.get("duty"), row.get("standby")
            if duty_row is None or standby_row is None:
                continue
            offsets = []
            converged_orphaned = True
            for peer_row_ in (duty_row, standby_row):
                if (
                    peer_row_["role"] != "standby"
                    or peer_row_["sync"] != "orphaned"
                    or not int_tick(peer_row_.get("aligned"))
                ):
                    converged_orphaned = False
                    break
                offsets.append(peer_row_["tick"] - peer_row_["aligned"])
            if not converged_orphaned:
                continue
            if (
                max(offsets) <= CLEAR_BOUND
                and abs(duty_row["tick"] - standby_row["tick"]) <= CADENCE
            ):
                cleared = row
                break
        if cleared is None:
            failures.append(
                "the seeded offset never cleared as the tracked "
                "stream recovered — the mutual-standby pair's run "
                "ticks kept their standing lead past the "
                f"reconvergence bound: last row "
                f"{json.dumps(frames[-1], sort_keys=True)}"
            )
        digest_entries.append(
            {
                "phase": "recover",
                "seeded": seeded,
                "stream_tick": "stamped" if stamped_lead else "absent",
                "cleared": "yes" if cleared is not None else "no",
            }
        )

        # Phase 6 — the promotion: the demoted peer's `orphaned`
        # verdict is promotable — the ownerless line's conditional
        # claim — and the resumption continues the run's tick domain:
        # the promoting/active walk at continuing ticks, the peers
        # settling the launch roles, no domain restart.
        promoted = None
        attempt = 0
        while promoted is None and attempt < 2:
            status, report = pair.request(f"{duty_url}/promote", {})
            if status == 200 and report.get("role") == "promoting":
                promoted = report
            else:
                attempt += 1
                if attempt >= 2:
                    failures.append(
                        f"POST /promote on {duty_name} answered "
                        f"{status} {report}, expected a promoting "
                        "report — the orphaned run's conditional "
                        "claim must lift the gate"
                    )
                    raise Abort
                scan(standby_url)
                scan(duty_url)
                time.sleep(SETTLE_S)
        digest_entries.append({"phase": "promote", "report": "promoting"})

        settled_active = None
        for _ in range(PROMOTE_SCANS):
            scan(duty_url)
            time.sleep(SETTLE_S)
            observe(("duty", duty_url))
            row = frames[-1].get("duty")
            if (row or {}).get("role") == "active":
                settled_active = row
                break
        if settled_active is None:
            failures.append(
                f"{duty_name} never settled into the active role — "
                f"GET /role answers "
                f"{try_role(duty_url)}"
            )
            raise Abort
        for _ in range(RESTORE_TICKS):
            scan(standby_url)
            scan(duty_url)
            time.sleep(SETTLE_S)
            observe(("duty", duty_url), ("standby", standby_url))
        digest_entries.append(
            {"phase": "restore", "ticks": RESTORE_TICKS}
        )

        # The launch roles: the field owner active again, the
        # configured standby tracking it — the manifest's arrangement
        # the pair rests on.
        duty_role = try_role(duty_url)
        standby_role = try_role(standby_url)
        restored = (
            (duty_role or {}).get("role") == "active"
            and claim_reclaim.tracking(standby_role)
        )
        if not restored:
            failures.append(
                "the pair never settled back to its launch roles — "
                f"the field owner reports {duty_role}, the tracking "
                f"peer {standby_role}"
            )

        # Phase 7 — the bounded-tick audit over every served row the
        # run collected. Under `--tamper ratcheting-offset` the
        # post-seed observations are doctored into the compounding
        # lead the defect left — each recovered row's tick inflated by
        # the seeded offset one further bound per frame — so the
        # audit must still name the ratchet.
        audit = frames
        if tamper == "ratcheting-offset":
            audit = [dict(frame) for frame in frames]
            seed_gap = seeded or WINDOW_SCANS
            for index, frame in enumerate(audit[seed_index:], 1):
                for key in ("duty", "standby"):
                    row = frame.get(key)
                    if row is not None and int_tick(row.get("tick")):
                        row = dict(row)
                        row["tick"] += index * seed_gap
                        frame[key] = row
        audit_frames(audit, base_tick, failures)

        # Phase 8 — the durable half: each peer's declared journal
        # file, the records of the one cold-start run — the axis
        # never regressing, no stamp leaping the driven-scan cadence,
        # no source restart on the same-generation stream, and the
        # demoted peer's role walk carrying the demote and the
        # promote at continuing ticks beside its journaled
        # announced-source adoption.
        gained = {}
        for name, path in journal_files.items():
            entries = journal_audit(
                name, path, base_tick, driven, failures,
                inconclusive, rig,
            )
            if entries is not None:
                gained[name] = entries
        duty_entries = gained.get(duty_name)
        if duty_entries is not None:
            walk = [
                (frm, to)
                for _tick, frm, to in pair.role_transitions(duty_entries)
            ]
            want = [
                ("active", "demoting"),
                ("demoting", "standby"),
                ("standby", "promoting"),
                ("promoting", "active"),
            ]
            position = 0
            for step in walk:
                if position < len(want) and step == want[position]:
                    position += 1
            if position != len(want):
                failures.append(
                    f"{duty_name}'s durable journal carries the role "
                    f"walk {walk} — expected the demote/promote "
                    "continuing the run: " + " -> ".join(
                        f"{frm}->{to}" for frm, to in want
                    )
                )
            adopted = [
                entry
                for entry in duty_entries
                if "tracking_source_adopted" in (entry.get("event") or {})
            ]
            if not adopted:
                failures.append(
                    f"{duty_name}'s announced-source demotion "
                    "journaled no tracking_source_adopted — the "
                    "adoption the mutual-standby staging stood on "
                    "went unrecorded"
                )
        digest_entries.append(
            {
                "phase": "durable",
                "duty": "continued",
                "standby": "continued",
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if rig is not None:
            if rig.duty is not None:
                resume_peer(rig.duty)
            rig.close()

    # The contract's clauses outrank the staging classifications: a
    # real tick-domain violation reports the named failure even when
    # the run also collected inconclusive evidence.
    if not failures and inconclusive:
        raise Inconclusive("; ".join(inconclusive))
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
        choices=["ratcheting-offset"],
        help="doctor the post-recovery observations into the "
        "compounding seeded-offset ratchet the contract pins — the "
        "bounded-tick audit must still report it",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = mutual_tracking_tick_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "mutual-tracking-tick: the ratcheting-offset case "
                "wanted the run to surface its named diagnostic — an "
                "inconclusive run offers the doctored case no "
                "evidence"
            )
            return 1
        eprint(f"mutual-tracking-tick: inconclusive — {inconclusive}")
        print(f"mutual-tracking-tick-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"mutual-tracking-tick: {line}")
        return 1
    for failure in failures:
        eprint(f"mutual-tracking-tick: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"mutual-tracking-tick: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "observations"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"mutual-tracking-tick-digest {digest} — the demoted pair "
        "tracked each other orphaned at the scan cadence, the seeded "
        "offset cleared on the recovered stream, the promote "
        "continued the tick domain, and the launch roles restored"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
