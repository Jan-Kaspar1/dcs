#!/usr/bin/env python3
"""The stale-budget-cadence leg for the reference plant — the
consumer-boundary mirror of the qa rig's cadence-domain leg, pinning
the contract #1411's fix declares on the customer-owned pair
(WW-ENG-003, WW-OPS-003).

The rig pins the contract; this repository never did. The shape the
contract is about is the one a redundant pair runs by definition: a
tracking standby scanning faster than the field owner steps its inputs.
A declared freshness budget is measured in the *reader's* run ticks, so
before #1411 a peer whose scan cadence outpaced the field owner's step
cadence read the identical driver report on every scan in between, and
a budget below the owner's step period presented a healthy field as
stale and good again on every field step — a `quality_changed` pair per
step, flapping `Uncertain` to every alarm and page consumer, from the
same two-tick-domains class as the retired claim-skew bound. The
declared patience is now the greater of the declared budget and the
arrival period the reader has itself demonstrated on the point.

This leg is the mirror of the qa rig's stale-budget-cadence leg under
the file-discovered convention, so the stage's diagnostics land on
this file's `stale-budget-cadence-*` stem and adding it edits no
`check.sh`, the boundary lint, or the harness.

Two arms, two leg-spawned readers, one pass — the cadence is the only
variable between them, and it is *declared*, not assumed:

- the **control** reader is paced like the field owner: one reader scan
  per owner scan, so the demonstrated arrival period is the owner's own
  step period measured in reader ticks. Freezing the writer — the
  writer-holding peer's `SIGSTOP`, which stops the shared field's
  stepping under its single-writer claim — must still age the
  composition's budgeted field input to `Uncertain(Stale)` at the
  declared lag while its unbudgeted neighbour keeps `Good`. That is the
  declared staleness behavior the widening must leave alone, and it is
  where the leg *measures* the declared budget off the customer's own
  composition: the reader-tick age at which the frozen field's
  budgeted point first presents stale is the declared floor the
  subject arm is judged against.
- the **subject** reader runs `CADENCE_RATIO` scans per owner scan —
  the harness's per-member `--dt` cadence lever — so the field's
  arrival period in the subject's own run ticks is that ratio. The leg
  reads both seats' served run ticks across the window and requires the
  measured ratio to clear the recorded staging bound, since a reader
  paced like the owner says nothing about one that is not. Once the
  reader has watched the field publish twice — the arrival evidence the
  contract's own cold-start limit names — the judged window opens, and
  inside it the budgeted point must present `Good` on every served
  sample while the journaled `quality_changed` traffic for it stays
  inside the bound the contract names: nothing after the arrival
  evidence.

The witness is what makes the cadence-domain claim measurable on the
served surface: a reader cannot see the driver report's own stamp,
which is minted in the field owner's step domain, but it does see
every value that step moves. The reader-tick gaps between the
unbudgeted neighbour's served changes are the field's demonstrated step
period expressed in the reader's own run ticks — the same-domain
quantity the contract judges the verdict in. Both judgments ride the
judged window and deliberately so: before the arrival evidence exists
the contract answers a peer to the declared budget alone, which is the
cold-start limit decision 45 records.

The deployed pair never enters the staging beyond the shared field the
two arms read: the readers are leg-spawned peers of the field owner,
and the pair's launch roles are restored before the stage moves on.

A pinned release predating the contract, or a harness that admits no
asymmetric pacing, reports `stale-budget-cadence-digest
inconclusive` — never a vacuous pass.

Usage:

    stale_budget_cadence.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `stale-budget-cadence-digest <sha256>` line prints —
the check runs two passes and compares them
(`stale-budget-cadence-nondeterministic`). A contract violation
reports `stale-budget-cadence: …` lines on stderr and exits 1 — the
check's `stale-budget-cadence-failed`. `--tamper expect-patience`
doctors the leg's expectation to the defect's opposite — the widened
patience treated as unbounded, freshness asserted over a genuine
starvation — so the pass must fail naming the stale presentation the
honest control arm observed, proving the leg's budget clause is
exercised rather than assumed.
"""

import argparse
import json
import os
import signal
import sys
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import claim_reclaim
import driver_recovery
import pair
import simulate
import stranded_rejoin


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg treating the widened patience as unbounded —
# asserting freshness over a genuine starvation — must surface the
# named diagnostic on the honest control arm's stale presentation
# rather than passing an unexercised contract.
LEG = {
    # The next free slot in the stage's recorded order (880 is the
    # orphan-episode-bound leg); no two legs may share one.
    "order": 890,
    "title": "the stale-budget-cadence leg",
    "passes": "stale-budget-cadence-leg",
    "tampers": [
        {
            "name": "expect-patience",
            "passed": "an expect-patience case passed the "
            "stale-budget-cadence leg",
            "missed": "the expect-patience case did not report its "
            "named diagnostic",
            "evidence": [
                "the doctored expectation wanted freshness over a starvation the declared budget must catch",
            ],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates — or the harness never covers — the
    contract the leg exercises: the run classifies inconclusive, never
    a product failure. The first arg is the stable reason the
    `inconclusive` digest line prints (two identical passes must share
    it); the optional second arg the run's own evidence, reported on
    stderr only, where endpoints and ticks belong."""


# The reader scans per owner scan the subject arm is paced at — the
# per-member cadence lever. Four is "several times the field owner's
# step cadence" and sits under the rig's own 10:1 lever.
CADENCE_RATIO = 4
# The measured asymmetry the subject arm's staging must clear: a reader
# several times faster than the field owner, read off the served run
# ticks rather than assumed from the launch arguments.
MIN_MEASURED_RATIO = 2
# The witnessed publications the subject arm waits for before its
# judged window opens — two gaps complete the two-deep arrival window
# the contract itself holds.
ARRIVAL_WARM = 2
# The judged window's rounds, each one owner scan plus the subject's
# ratio of scans, and the control arm's hold past its first stale
# verdict — the relapse check.
WINDOW_ROUNDS = 6
CONTROL_HOLD = 6
CONTROL_WARM = 6
# The recovery bounds: the resumed writer returning each arm's
# budgeted point to Good, and the pair's settle train.
RECOVERY_SCANS = 12
SETTLE_SCANS = 4
# The margin the declared budget's measurement domain allows the
# control arm's first stale reading: past the declared budget, and no
# more than this many reader ticks beyond it.
LAG_SLACK = 4

# The doctor's stable evidence prefix.
TAMPER_EVIDENCE = (
    "the doctored expectation wanted freshness over a starvation the "
    "declared budget must catch"
)


def http(url, body=None, what="request"):
    """GET/POST a JSON endpoint under a generous bound — the served
    surface read through one bounded path."""
    try:
        if body is None:
            with urllib.request.urlopen(url, timeout=30) as response:
                return json.load(response)
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise Abort(f"{what} on {url} answered {error}")


def quality_key(quality):
    """The served quality as one stable word — `good`, `stale`,
    `bad:<reason>`, or `missing` where the sample carries none."""
    if quality == "good":
        return "good"
    if isinstance(quality, dict):
        for kind, reason in quality.items():
            return f"{kind}:{reason}"
    return "missing"


def freeze(process):
    """`SIGSTOP` on the writer-holding peer's process — the harness's
    stop/pause lever, and under the shared field's single-writer claim
    exactly a stopped writer: the field stops stepping while a
    surviving reader's reads keep answering."""
    if process is None or process.poll() is not None:
        raise Abort("the writer-holding peer's process is not running")
    if not hasattr(signal, "SIGSTOP"):
        raise Abort("this platform has no SIGSTOP to freeze a peer with")
    process.send_signal(signal.SIGSTOP)


def thaw(process):
    """`SIGCONT` — the frozen writer resumes and, driven again, steps
    the field once more."""
    if process is not None and process.poll() is None:
        process.send_signal(signal.SIGCONT)


def surface(model):
    """The composition's freshness contract as `(budgeted, budget,
    unbudgeted)` — the field-bound `in` point carrying
    `stale_after_ticks`, its declared budget, and an unbudgeted
    field-bound `in` neighbour sharing the same device's field step.
    None where the composition carries no such declaration."""
    points = {entry["id"]: entry for entry in model.get("io_points") or []}
    budgeted = [
        entry
        for entry in points.values()
        if entry.get("stale_after_ticks") is not None
        and entry.get("direction") == "in"
        and entry.get("channel")
    ]
    if len(budgeted) != 1:
        return None
    point = budgeted[0]
    neighbour = next(
        (
            entry
            for entry in points.values()
            if entry.get("direction") == "in"
            and entry.get("channel", {}).get("device")
            == point["channel"]["device"]
            and entry.get("stale_after_ticks") is None
        ),
        None,
    )
    if neighbour is None:
        return None
    return point, point["stale_after_ticks"], neighbour


def served_sample(snapshot, point):
    """One point's served sample out of a `GET /snapshot` answer."""
    for entry in (snapshot or {}).get("points") or []:
        if entry.get("point") == point:
            return entry.get("sample") or {}
    return {}


def observation(url, budgeted, witness):
    """One reader-tick observation: the served run tick, the budgeted
    point's quality word, the unbudgeted neighbour's, and the witness's
    served value as a stable string. The value is the publication marker
    a reader can read — the driver report's own stamp is minted in the
    field owner's step domain and never crosses onto this surface."""
    role = http(f"{url}/role", None, "GET /role")
    snapshot = http(f"{url}/snapshot", None, "GET /snapshot")
    return {
        "tick": role.get("tick"),
        "budgeted": quality_key(
            served_sample(snapshot, budgeted).get("quality")
        ),
        "unbudgeted": quality_key(
            served_sample(snapshot, witness).get("quality")
        ),
        "witness": json.dumps(
            served_sample(snapshot, witness).get("value"), sort_keys=True
        ),
    }


def gaps(rows):
    """The reader-tick gaps between the witness's served changes — the
    field's demonstrated arrival period in the reader's own run domain,
    the same-domain quantity the contract judges freshness in."""
    marks = [
        row["tick"]
        for previous, row in zip(rows, rows[1:])
        if isinstance(previous["tick"], int) and isinstance(row["tick"], int)
        and row["witness"] != previous["witness"]
    ]
    return [later - earlier for earlier, later in zip(marks, marks[1:])]


def quality_changes(path, point):
    """The `quality_changed` records a reader's `--journal-file` carries
    for `point` — `(seq, tick, quality word)` triples in journal order.
    The durable half of the cadence clause: a healthy field flapping per
    field step is exactly what fills this file."""
    if not path or not os.path.exists(path):
        return []
    return [
        (
            entry.get("seq"),
            entry.get("tick"),
            quality_key(
                (entry.get("event") or {})
                .get("quality_changed", {})
                .get("to")
            ),
        )
        for entry in stranded_rejoin.journal_entries(path)
        if (entry.get("event") or {}).get("quality_changed", {}).get("point")
        == point
    ]


def spawn_reader(args, rig, name, standby, cadence):
    """Spawn one arm's reader: a controller the leg launches as a
    tracking peer of the field owner at its own `--dt` cadence, carrying
    its own `--journal-file` under the rig's scratch root. The cadence
    is the per-member lever the asymmetry is staged through: the owner's
    own `args.dt` for the control arm, `args.dt / cadence` for the
    subject. Returns `(process, url, journal_path, preamble)`."""
    root = os.path.join(rig.scratch, name)
    os.makedirs(root, exist_ok=True)
    files = {"journal_file": os.path.join(root, "journal.jsonl")}
    process, url, preamble = pair.spawn_peer(
        args.controller,
        args.model,
        args.dt / cadence,
        rig.plant_addr,
        standby,
        files,
        pair_token=pair.PAIR_TOKEN,
    )
    return process, url, files["journal_file"], preamble


def converge_reader(url, duty_url, failures, count=8):
    """Drive the field owner until the reader reports a converged
    tracking standby — the reader's own convergence on the shared line.
    Returns the reader's final role report, or None."""
    report = None
    for _ in range(count):
        http(f"{duty_url}/scan", {"scans": 1}, "POST /scan")
        http(f"{url}/scan", {"scans": 1}, "POST /scan")
        report = http(f"{url}/role", None, "GET /role")
        if claim_reclaim.tracking(report):
            return report
    return report


def stale_budget_cadence_pass(args, tamper):
    """The exercised run: converge the declared pair, spawn the
    control and subject readers on the shared field, measure the
    cadence asymmetry off their served run ticks, hold the control arm
    across the declared staleness behavior, and judge the subject arm
    inside the window its arrival evidence opens — with the durable
    journal's `quality_changed` traffic inside the contract's bound —
    restoring the launch layout after each arm.
    Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the contract or the
    harness admits no asymmetric pacing."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "stale-budget-cadence leg has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    contract = surface(model)
    if contract is None:
        raise Inconclusive(
            "the emitted composition declares no single freshness budget "
            "on a field input beside an unbudgeted neighbour sharing its "
            "field step",
            "the composition carries "
            f"{len(model.get('io_points') or [])} io points",
        )
    budgeted, budget, witness = contract
    digest_entries, evidence, failures = [], {}, []
    rig = None
    readers = []
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        evidence["surface"] = {
            "budgeted": budgeted["id"],
            "budget": budget,
            "witness": witness["id"],
            "ratio": CADENCE_RATIO,
        }

        # Phase 1 — the declared pair converged, then the two arms'
        # readers launched on the shared field: the control reader at
        # the owner's own cadence, the subject at the ratio the
        # per-member lever paces it at.
        converged = rig.converge(failures, 4)
        if failures:
            raise Abort
        control, control_url, control_journal, preamble = spawn_reader(
            args, rig, "control-reader", duty_url.removeprefix("http://"), 1
        )
        readers.append(control)
        if control_url is None:
            failures.append(
                "the control reader exited at startup: "
                f"{'; '.join(preamble) or 'no diagnostic'}"
            )
            raise Abort
        subject, subject_url, subject_journal, preamble = spawn_reader(
            args, rig, "subject-reader", duty_url.removeprefix("http://"),
            CADENCE_RATIO,
        )
        readers.append(subject)
        if subject_url is None:
            failures.append(
                "the subject reader exited at startup: "
                f"{'; '.join(preamble) or 'no diagnostic'}"
            )
            raise Abort
        for url in (control_url, subject_url):
            report = converge_reader(url, duty_url, failures)
            if not claim_reclaim.tracking(report):
                failures.append(
                    f"a reader never converged tracking on the field "
                    f"owner — its served role reports stayed {report}"
                )
                raise Abort
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"].get("role"),
                "standby_role": converged["standby_role"].get("role"),
                "arms": ["control", "subject"],
            }
        )

        # Phase 2 — the declared asymmetry, read off both seats' served
        # run ticks: the subject's tick gain against the owner's across
        # the same rounds. The staging is declared, not assumed — a
        # reader paced like the owner says nothing about one that is not.
        owner_mark = http(f"{duty_url}/role", None, "GET /role").get("tick")
        subject_mark = http(
            f"{subject_url}/role", None, "GET /role"
        ).get("tick")
        for _ in range(WINDOW_ROUNDS):
            http(f"{duty_url}/scan", {"scans": 1}, "POST /scan")
            for _ in range(CADENCE_RATIO):
                http(f"{subject_url}/scan", {"scans": 1}, "POST /scan")
        owner_now = http(f"{duty_url}/role", None, "GET /role").get("tick")
        subject_now = http(
            f"{subject_url}/role", None, "GET /role"
        ).get("tick")
        owner_gain = owner_now - owner_mark
        subject_gain = subject_now - subject_mark
        measured = subject_gain / owner_gain if owner_gain else 0.0
        evidence["asymmetry"] = {
            "owner_gain": owner_gain,
            "subject_gain": subject_gain,
            "measured": measured,
        }
        if owner_gain <= 0 or subject_gain <= owner_gain * MIN_MEASURED_RATIO:
            raise Inconclusive(
                "the harness admitted no asymmetric pacing — the subject "
                "reader's served run tick did not outpace the field "
                "owner's by the recorded staging bound, so the leg has no "
                "cadence-domain case to judge",
                f"the owner gained {owner_gain} run ticks while the "
                f"subject gained {subject_gain}",
            )
        digest_entries.append(
            {
                "phase": "asymmetry",
                "owner_gain": owner_gain,
                "subject_gain": subject_gain,
                "measured": round(measured, 3),
            }
        )

        # Phase 3 — the control arm's declared staleness behavior: the
        # writer frozen so the field stops stepping, the paced-like-the-
        # owner reader watching its budgeted point age out at the
        # declared lag while its unbudgeted neighbour holds Good. This
        # is where the leg measures the declared budget off the
        # customer's own composition.
        freeze(rig.duty)
        control_rows = []
        try:
            for _ in range(CONTROL_HOLD):
                http(f"{control_url}/scan", {"scans": 1}, "POST /scan")
                control_rows.append(
                    observation(
                        control_url, budgeted["id"], witness["id"]
                    )
                )
        finally:
            thaw(rig.duty)
        evidence["control"] = control_rows
        first_stale = next(
            (index for index, row in enumerate(control_rows)
             if row["budgeted"] == "uncertain:stale"),
            None,
        )
        digest_entries.append(
            {
                "phase": "control",
                "budget": budget,
                "first_stale": first_stale,
                "rows": control_rows,
            }
        )
        if first_stale is None:
            raise Inconclusive(
                "the paced-like-the-owner reader never presented the "
                "frozen field's budgeted point as Uncertain(Stale) — the "
                "composition's declared budget is inert on the launched "
                "tooling",
                f"the served rows were {control_rows}",
            )
        lag = first_stale + 1
        evidence["control_lag"] = lag
        if lag <= budget or lag > budget + LAG_SLACK:
            failures.append(
                f"the control reader first presented stale {lag} served "
                f"rows into the frozen window against a declared budget "
                f"of {budget} (margin {LAG_SLACK}) — the declared "
                f"staleness behavior must hold at the declared lag: "
                f"{control_rows}"
            )
            raise Abort
        if any(row["unbudgeted"] != "good" for row in control_rows):
            failures.append(
                "the unbudgeted neighbour presented non-Good inside the "
                f"control window ({witness['id']}) — the declared "
                f"budget is a per-point declaration: {control_rows}"
            )
            raise Abort
        if tamper == "expect-patience":
            failures.append(
                f"{TAMPER_EVIDENCE} — the honest control arm's frozen "
                f"field presented {control_rows[first_stale]['budgeted']} "
                f"on the budgeted point ({budgeted['id']}) {lag} rows "
                "into the window, against the declared budget of "
                f"{budget}"
            )
            raise Abort
        relapsed = next(
            (row for row in control_rows[first_stale:]
             if row["budgeted"] == "good"),
            None,
        )
        if relapsed is not None:
            failures.append(
                "the budgeted point returned to Good while the control "
                "arm's field stayed frozen — the stale verdict flickered "
                f"rather than holding: {control_rows[first_stale:]}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "declared",
                "lag": lag,
                "budget": budget,
                "unbudgeted": "good",
                "verdict": "degraded",
            }
        )

        # The control arm's recovery: the resumed writer driven again, so
        # the field resumes stepping and the budgeted point returns Good
        # — the declared staleness behavior ends there too.
        recovered = None
        for _ in range(RECOVERY_SCANS):
            http(f"{duty_url}/scan", {"scans": 1}, "POST /scan")
            http(f"{control_url}/scan", {"scans": 1}, "POST /scan")
            row = observation(control_url, budgeted["id"], witness["id"])
            if row["budgeted"] == "good":
                recovered = row
                break
        evidence["control_recovered"] = recovered
        if recovered is None:
            raise Inconclusive(
                "the resumed writer never returned the control reader's "
                "budgeted point to Good inside the recovery bound",
                f"the last served row was {row}",
            )

        # Phase 4 — the subject arm: once its own arrival evidence
        # exists — the witnessed publications whose reader-tick gaps are
        # the field's demonstrated period — the judged window opens, and
        # inside it the budgeted point must present Good on every served
        # sample rather than flapping stale/good per field step.
        warm = []
        while len(gaps(warm)) < ARRIVAL_WARM and len(warm) < CONTROL_WARM * 8:
            http(f"{duty_url}/scan", {"scans": 1}, "POST /scan")
            for _ in range(CADENCE_RATIO):
                http(f"{subject_url}/scan", {"scans": 1}, "POST /scan")
            warm.append(
                observation(subject_url, budgeted["id"], witness["id"])
            )
        evidence["warm"] = warm
        demonstrated = gaps(warm)
        evidence["demonstrated"] = demonstrated
        if len(demonstrated) < ARRIVAL_WARM:
            raise Inconclusive(
                "the subject reader never witnessed the field publish "
                f"{ARRIVAL_WARM + 1} times — the arrival evidence the "
                "contract judges the widened patience in never arrived",
                f"the warm rows were {warm}",
            )
        if max(demonstrated) <= budget:
            raise Inconclusive(
                "the subject reader's demonstrated arrival period never "
                f"cleared the declared budget of {budget} — no cadence "
                "asymmetry is present to judge",
                f"the demonstrated gaps were {demonstrated}",
            )
        mark = warm[-1]["tick"]
        # The durable floor is taken here, where the arrival evidence
        # exists: the contract allows the reader's cold-start pair --
        # the transitions it lands before it has watched the field
        # publish twice -- and nothing after it. Counting the records
        # the cold start already left, rather than comparing against a
        # reader tick, keeps the assertion on what the judged window
        # adds: no new `quality_changed` for the budgeted point once
        # the arrival evidence is in.
        cold_start = quality_changes(subject_journal, budgeted["id"])
        rows = []
        for _ in range(WINDOW_ROUNDS):
            http(f"{duty_url}/scan", {"scans": 1}, "POST /scan")
            for _ in range(CADENCE_RATIO):
                http(f"{subject_url}/scan", {"scans": 1}, "POST /scan")
                rows.append(
                    observation(subject_url, budgeted["id"], witness["id"])
                )
        evidence["judged"] = rows
        flap = next(
            (row for row in rows if row["budgeted"] != "good"), None
        )
        digest_entries.append(
            {
                "phase": "judged",
                "mark": mark,
                "demonstrated": demonstrated,
                "budget": budget,
                "flap": flap,
                "rows": rows,
            }
        )
        if flap is not None:
            failures.append(
                "the subject reader — scanning "
                f"{CADENCE_RATIO} times per field step — presented "
                f"{flap['budgeted']} on the budgeted point "
                f"({budgeted['id']}) at reader tick {flap['tick']}, "
                "inside the window its demonstrated arrival period of "
                f"{max(demonstrated)} reader ticks opens — freshness must "
                "be judged in the declared domain, not the reader's raw "
                f"tick count: {rows}"
            )
            raise Abort
        # The durable half: the reader's own `--journal-file` may carry
        # the cold-start transitions before the arrival evidence, and
        # nothing after it — the bound the contract names.
        changes = quality_changes(subject_journal, budgeted["id"])
        evidence["quality_changed"] = changes
        evidence["cold_start"] = cold_start
        added = changes[len(cold_start):]
        if added:
            failures.append(
                "the subject reader journaled "
                f"{len(added)} further quality_changed records for the "
                f"budgeted point ({budgeted['id']}) inside the judged "
                "window — the arrival evidence its demonstrated "
                f"{max(demonstrated)}-reader-tick period opened leaves "
                "the journaled quality traffic at zero there: "
                f"{added}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "subject",
                "mark": mark,
                "budgeted": "good",
                "cold_start": len(cold_start),
                "journal_after_evidence": 0,
                "demonstrated": max(demonstrated),
            }
        )

        # Phase 5 — the pair's launch roles restored, the readers torn
        # down, so the stage's next leg meets the declared layout.
        if not driver_recovery.roles_hold(
            rig, failures, "after the cadence arms were judged"
        ):
            raise Abort
        handover = []
        for _ in range(SETTLE_SCANS):
            _tracked, owner_snapshot = rig.tick(
                standby_url, duty_url, failures,
                diverged="the pair's images diverged at tick {tick} — "
                "the cadence arms were not read-only",
            )
            handover.append(owner_snapshot["tick"])
        digest_entries.append(
            {
                "phase": "restore",
                "ticks": handover,
                "duty": "active",
                "standby": "tracking",
                "sync": stranded_rejoin.sync_kind(
                    http(f"{standby_url}/role", None, "GET /role")
                ),
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        for reader in readers:
            pair.stop(reader)
        if rig is not None:
            thaw(rig.duty)
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
        choices=["expect-patience"],
        help="doctor the leg's expectation to the defect's opposite — "
        "the widened patience treated as unbounded, freshness asserted "
        "over a genuine starvation — so the pass must fail naming the "
        "stale presentation the honest control arm observed",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = stale_budget_cadence_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"stale-budget-cadence: {TAMPER_EVIDENCE} — an "
                "inconclusive run offers the doctored case no evidence"
            )
            return 1
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"stale-budget-cadence: inconclusive — {detail}")
        print(f"stale-budget-cadence-digest inconclusive — {reason}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"stale-budget-cadence: {line}")
        return 1
    for failure in failures:
        eprint(f"stale-budget-cadence: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"stale-budget-cadence: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"stale-budget-cadence-digest {digest} — the subject reader ran "
        f"{evidence['asymmetry']['measured']:.1f}x the field owner's "
        "cadence and, inside the window its "
        f"{max(evidence['demonstrated'])}-reader-tick demonstrated "
        f"arrival period opens past the declared budget of "
        f"{evidence['surface']['budget']} on point "
        f"{evidence['surface']['budgeted']}, held Good with no "
        "journaled quality_changed traffic after the arrival evidence, "
        f"while the paced-like-the-owner reader's declared staleness "
        f"behavior held at {evidence['control_lag']} rows — the pair's "
        "launch roles stand"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())