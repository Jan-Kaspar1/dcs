#!/usr/bin/env python3
"""The stale-freshness stage for the reference plant — the
consumer-side mirror of the qa rig's declared-freshness-budget leg,
pinning decision 45's `stale_after_ticks` on the *customer's own*
station composition and on the deployment the manifest declares.

The consumer's station composition is its own source: the rig-side
fixture's budgeted point is not the customer's, so this repository
declares the same small budget on its own budgeted measurement — the
primary wet-well level, comfortably under the manifest's declared
`failover_budget` so staleness presents inside the writer-loss window
— and leaves its backup level beside it unbudgeted. That declared
pair is the site's freshness contract: one budgeted field input and
one unbudgeted neighbour sharing a single field step, so a held field
read is legible as degraded on the first and healthy on the second.

The stage brings the manifest-declared pair up through the pair stage's
shared rig (`ci/legs/pair.py`'s `launch_pair`/`PairRig` — the same
bring-up every `ci/legs/` leg uses, so the stage and the legs run one
pair definition), unarmed, and reads the served surface of the
*surviving* peer:

- the contract surface, read off the emitted model rather than assumed:
  the budgeted point is a field-bound `in` carrying
  `stale_after_ticks`, the model's connections wire it into a
  `failover-select`'s `primary` port, and an unbudgeted field `in`
  neighbour on the same device is its contrast. Each absence is the
  consumer's composition not carrying the contract — the stage reports
  `stale-freshness-digest inconclusive`, never a failure;
- convergence on the manifest's declared wiring, then a pre-freeze
  baseline in which both probes read `Good`;
- the writer-loss induction: the writer-holding peer's process
  `SIGSTOP`-frozen. The shared simulated plant steps only through its
  write claim, so with the writer frozen the field stops stepping
  while the surviving peer's reads keep answering — the field stamp
  lags the scan while the transport stays healthy, which is the shape
  a declared freshness budget judges. (Stopping the writer peer is the
  honest lever precisely because it leaves the claim standing: the
  surviving peer keeps reading the field it is told is there, and the
  budget — not the transport — is what answers.) The surviving peer is
  driven by `POST /scan`, one scan per served row;
- the walk: the budgeted point must go `Good` → `Uncertain(Stale)` at
  the declared lag measured in the surviving peer's own run ticks — a
  reader-tick age past the declared budget, and within the margin the
  documented arrival-period widening allows — while the unbudgeted
  neighbour keeps serving its last sample `Good` throughout. The stale
  presentation must be degraded, never a healthy last-known value
  (WW-ALM-003), and control must react through the wiring the model
  declares rather than holding the last-good reading: the
  `failover-select` moves onto its backup and its `backup_active`
  annunciation asserts;
- the relapse check — the stale verdict must persist across the rest of
  the bounded window, not flicker;
- the recovery: the writer thawed and driven again, so the field
  resumes stepping and the budgeted point returns `Good` with the
  annunciation falling silent;
- the durable evidence: the surviving peer's served `/history` for the
  budgeted point retains the stale interval between healthy samples —
  the record the outage is read back through after the fact;
- the pair's launch roles restored: the field owner `active`, the
  declared standby `tracking` it.

Usage:

    stale_freshness.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json \
        [--tamper expect-last-good]

On success one `stale-freshness-digest <sha256>` line prints —
`ci/check.sh` runs two passes and compares them
(`stale-freshness-nondeterministic`). A contract violation reports
`stale-freshness: …` lines on stderr and exits 1 — the check's
`stale-freshness-failed`. `--tamper expect-last-good` doctors the
stage's expectation to the defect shape — the held read served as a
healthy last-known value, WW-ALM-003's forbidden presentation — so the
pass must fail naming the degraded presentation it observed, proving
the assertion fires on the honest run rather than passing an
unexercised contract (`stale-freshness-unchecked`).
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
sys.path.insert(0, os.path.join(_HERE, "legs"))

import claim_reclaim
import driver_recovery
import pair
import simulate
import stranded_rejoin


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The consumer's own composition does not carry the contract —
    the stage classifies inconclusive, never a product failure. The
    first arg is the stable reason the `inconclusive` digest line
    prints (two identical passes must share it); the optional second
    arg the run's own evidence, reported on stderr only."""


# The driven-scan bounds the stage's phases run. The declared pair
# converges `tracking` inside a few pulls; the writer-loss window
# spans the stale walk plus the relapse samples past it — dozens of
# reader ticks, far inside the pair's own bounds — and the recovery
# is a handful of scans on each peer.
CONVERGE_SCANS = 6
STALE_WATCH = 12
RECOVERY_SCANS = 12
SETTLE_SCANS = 4

# The margin the declared budget's measurement domain allows: the
# patience is the greater of the declared budget and the arrival period
# the reader has itself demonstrated on the point (#1411), so the
# first stale reading is asserted strictly past the declared budget and
# no more than this many reader ticks beyond it.
ARRIVAL_SLACK = 4

# The served monitor's per-request bound. The surviving peer's scans
# are the harness's own requests and answer promptly; the frozen
# writer's monitor is never addressed at all, so a generous bound
# costs nothing and keeps a wedged stage from hanging the check.
REQUEST_TIMEOUT = 30

# The doctor's stable evidence prefix — every failure the tampered
# pass records carries it, so the check's negative case finds it
# whether the honest run presented the stale read degraded or a
# predating composition offered the case no evidence at all.
TAMPER_EVIDENCE = (
    "the doctored expectation wanted the held read served as a healthy "
    "last-known value"
)


def http(url, body=None, what="request"):
    """GET/POST a JSON endpoint under `REQUEST_TIMEOUT` — the served
    surface read through one bounded path. A transport error or a
    refused request is data for the caller to classify, never an
    unbounded hang."""
    try:
        if body is None:
            with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as r:
                return json.load(r)
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT
        ) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise Abort(f"{what} on {url} answered {error}")


def quality_key(quality):
    """The served quality as one stable word — `good`, `stale`,
    `bad:<reason>`, or `missing` where the sample carries none. The
    digest reads the words, never the wire shapes."""
    if quality == "good":
        return "good"
    if isinstance(quality, dict):
        for kind, reason in quality.items():
            return f"{kind}:{reason}"
    return "missing"


def freeze(process):
    """`SIGSTOP` on the writer-holding peer's process — the harness's
    stop/pause lever. Its monitor socket stays bound but the run inside
    it stops, which under the shared field's single-writer claim is
    exactly a stopped writer: the field stops stepping while a
    surviving peer's reads keep answering."""
    if process is None or process.poll() is not None:
        raise Abort("the writer-holding peer's process is not running")
    if not hasattr(signal, "SIGSTOP"):
        raise Abort("this platform has no SIGSTOP to freeze a peer with")
    process.send_signal(signal.SIGSTOP)


def thaw(process):
    """`SIGCONT` — the frozen writer resumes and, driven again through
    `POST /scan`, steps the field once more."""
    if process is not None and process.poll() is None:
        process.send_signal(signal.SIGCONT)


def served_sample(snapshot, point):
    """One point's served sample out of a `GET /snapshot` answer, or
    None when the snapshot carries no such point."""
    for entry in (snapshot or {}).get("points") or []:
        if entry.get("point") == point:
            return entry.get("sample") or {}
    return None


def io_points(model):
    """The emitted model's `io_point` declarations keyed by id."""
    return {entry["id"]: entry for entry in model.get("io_points") or []}


def contract_surface(model):
    """The consumer composition's freshness contract, read off the
    emitted model: `(budgeted, budget, unbudgeted, backup_active)`.
    `budgeted` is the field-bound `in` point carrying
    `stale_after_ticks` that the model's connections wire into a
    `failover-select`'s `primary` port; `unbudgeted` is a field-bound
    `in` neighbour on the same device declaring none — the contrast the
    per-point declaration is legible against; `backup_active` is the
    point the selector's annunciation port is wired to, the control
    reaction the stale presentation must drive. Returns None when the
    composition carries no such declaration."""
    points = io_points(model)
    components = {
        entry["id"]: entry
        for entry in model.get("components") or []
    }
    wired = {}
    annunciated = {}
    for connection in model.get("connections") or []:
        port = (connection.get("to") or {}).get("port")
        source = (connection.get("from") or {}).get("point")
        if port is not None and source is not None:
            component = components.get(port.get("component"), {})
            if component.get("kind") == "failover-select":
                wired[port.get("name")] = source
            continue
        # The selector's own outputs are wired the other way round —
        # from a port to the point it drives.
        origin = (connection.get("from") or {}).get("port")
        target = (connection.get("to") or {}).get("point")
        if origin is not None and target is not None:
            component = components.get(origin.get("component"), {})
            if component.get("kind") == "failover-select":
                annunciated[origin.get("name")] = target
    primary = wired.get("primary")
    declaration = points.get(primary) if primary is not None else None
    if not declaration or declaration.get("stale_after_ticks") is None:
        return None
    if declaration.get("direction") != "in" or not declaration.get("channel"):
        return None
    device = declaration["channel"].get("device")
    neighbour = next(
        (
            entry
            for entry in points.values()
            if entry.get("direction") == "in"
            and entry.get("channel", {}).get("device") == device
            and entry.get("stale_after_ticks") is None
        ),
        None,
    )
    if neighbour is None:
        return None
    active = annunciated.get("backup_active")
    if active is None:
        return None
    return declaration, declaration["stale_after_ticks"], neighbour, active


def row(snapshot, budgeted, unbudgeted, active):
    """One served observation of the contract: the budgeted point's
    quality word, the unbudgeted neighbour's, and the annunciation —
    the row the walk judges and the digest normalizes."""
    budgeted_sample = served_sample(snapshot, budgeted)
    return {
        "budgeted": quality_key(budgeted_sample.get("quality")),
        "unbudgeted": quality_key(
            served_sample(snapshot, unbudgeted).get("quality")
        ),
        "backup_active": (
            served_sample(snapshot, active).get("value") or {}
        ).get("bool"),
    }


def stale_history(url, point):
    """One point's retained served history as `[seq, quality word]` pairs
    — the durable evidence the stale interval must survive in."""
    payload = http(f"{url}/history?point={point}", None, "GET /history")
    if not isinstance(payload, list):
        return []
    for entry in payload:
        if entry.get("point") == point:
            return [
                [
                    sample.get("seq"),
                    quality_key((sample.get("sample") or {}).get("quality")),
                ]
                for sample in entry.get("samples") or []
            ]
    return []


def stale_freshness_pass(args, tamper):
    """The exercised run: read the consumer composition's freshness
    contract off the emitted model, converge the manifest-declared
    pair, freeze the writer so the field stamp lags the scan, read the
    budgeted point's walk from `Good` to `Uncertain(Stale)` at the
    declared lag while its unbudgeted neighbour keeps serving, the
    annunciation asserting, the recovery back to `Good`, the retained
    stale interval, and the launch roles restored.
    Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the composition carries no such contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the stale-freshness "
            "stage has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    surface = contract_surface(model)
    if surface is None:
        raise Inconclusive(
            "the emitted composition declares no freshness budget on a "
            "field input the model's failover-select primary reads, "
            "beside an unbudgeted field input on the same device",
            "the composition carries "
            f"{sorted(point for point in io_points(model))} io points",
        )
    budgeted, budget, unbudgeted, active = surface
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        evidence["surface"] = {
            "budgeted": budgeted["id"],
            "budget": budget,
            "unbudgeted": unbudgeted["id"],
            "annunciation": active,
        }

        # Phase 1 — convergence on the manifest's declared wiring, and
        # the pre-freeze baseline: both probes read Good on the peer
        # that survives the writer's loss.
        converged = rig.converge(failures, CONVERGE_SCANS)
        if failures:
            raise Abort
        baseline = row(
            http(f"{standby_url}/snapshot", None, "GET /snapshot"),
            budgeted["id"],
            unbudgeted["id"],
            active,
        )
        evidence["baseline"] = baseline
        if baseline["budgeted"] != "good" or baseline["unbudgeted"] != "good":
            failures.append(
                "the surviving peer serves "
                f"{baseline} before the induction — both probes must "
                "read Good on a healthy field"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"].get("role"),
                "standby_role": converged["standby_role"].get("role"),
                "baseline": baseline,
            }
        )

        # Phase 2 — the writer-loss window: the writer-holding peer
        # frozen, so the field stops stepping under the surviving
        # peer's advancing scans while its reads keep answering.
        freeze(rig.duty)
        try:
            walk = []
            for _ in range(STALE_WATCH):
                http(f"{standby_url}/scan", {"scans": 1}, "POST /scan")
                snapshot = http(
                    f"{standby_url}/snapshot", None, "GET /snapshot"
                )
                walk.append(row(snapshot, budgeted["id"], unbudgeted["id"],
                                active))
        finally:
            thaw(rig.duty)
        evidence["walk"] = walk
        first_stale = next(
            (index for index, entry in enumerate(walk)
             if entry["budgeted"] == "uncertain:stale"),
            None,
        )
        digest_entries.append(
            {
                "phase": "walk",
                "budget": budget,
                "first_stale": first_stale,
                "walk": walk,
            }
        )
        if first_stale is None:
            raise Inconclusive(
                "the frozen field never aged the budgeted point to "
                "Uncertain(Stale) — the launched tooling predates the "
                "declared freshness budget the stage judges, or the "
                "consumer composition's declaration is inert",
                f"the served walk was {walk}",
            )
        if walk[first_stale - 1]["budgeted"] != "good":
            failures.append(
                "the budgeted point presented "
                f"{walk[first_stale - 1]['budgeted']} before its first "
                "stale reading — the walk must start from Good: "
                f"{walk}"
            )
            raise Abort
        # The declared lag: the first stale reading's position in the
        # window bounds the reader-tick age at which the held report
        # aged out, and it must sit past the declared budget — never
        # before it — and inside the margin the documented
        # arrival-period widening allows.
        lag = first_stale + 1
        evidence["lag"] = lag
        if lag <= budget or lag > budget + ARRIVAL_SLACK:
            failures.append(
                f"the budgeted point first presented stale {lag} served "
                f"reads into the window, against a declared budget of "
                f"{budget} (margin {ARRIVAL_SLACK}) — the verdict must "
                "be reached past the declared budget and no later than "
                f"the arrival-period widening allows: {walk}"
            )
            raise Abort
        # The unbudgeted neighbour is unaffected: it holds its last
        # sample and keeps reading Good for the whole window — the
        # per-point contract made legible by the contrast.
        drifted = next(
            (entry for entry in walk if entry["unbudgeted"] != "good"), None
        )
        if drifted is not None:
            failures.append(
                "the unbudgeted neighbour presented "
                f"{drifted['unbudgeted']} inside the writer-loss window "
                f"({unbudgeted['id']}) — the declared budget is a "
                f"per-point declaration, not a global rule: {walk}"
            )
            raise Abort
        # The doctored case — the stage asserting the held read served
        # as a healthy last-known value. The honest degraded
        # presentation must fail it.
        if tamper == "expect-last-good":
            failures.append(
                f"{TAMPER_EVIDENCE} — the honest walk presented "
                f"{walk[first_stale]['budgeted']} on the budgeted point "
                f"({budgeted['id']}) while its unbudgeted neighbour "
                f"({unbudgeted['id']}) kept reading "
                f"{walk[first_stale]['unbudgeted']}"
            )
            raise Abort
        # The relapse check: the stale verdict persists for the rest of
        # the bounded window rather than flickering back to healthy.
        relapsed = next(
            (entry for entry in walk[first_stale:]
             if entry["budgeted"] == "good"),
            None,
        )
        if relapsed is not None:
            failures.append(
                "the budgeted point returned to Good inside the "
                "writer-loss window while the field stayed frozen — the "
                "stale verdict flickered rather than holding: "
                f"{walk[first_stale:]}"
            )
            raise Abort
        # The control reaction: the model's declared wiring, not a held
        # reading. The failover-select moves onto its backup and its
        # annunciation asserts for as long as the primary is stale.
        silent = next(
            (entry for entry in walk[first_stale:]
             if entry["backup_active"] is not True),
            None,
        )
        if silent is not None:
            failures.append(
                "the failover-select's backup_active annunciation stayed "
                f"{silent['backup_active']} while the primary it reads "
                f"was stale — the composition's declared control wiring "
                "must react, never hold a last-good reading: "
                f"{walk[first_stale:]}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "stale",
                "lag": lag,
                "budget": budget,
                "unbudgeted": "good",
                "verdict": "degraded",
                "annunciation": "asserted",
            }
        )

        # Phase 3 — the recovery: the writer thawed and driven again, so
        # the field resumes stepping and the budgeted point returns to
        # Good with the annunciation falling silent.
        recovered = None
        handover = []
        for _ in range(RECOVERY_SCANS):
            http(f"{duty_url}/scan", {"scans": 1}, "POST /scan")
            http(f"{standby_url}/scan", {"scans": 1}, "POST /scan")
            snapshot = http(f"{standby_url}/snapshot", None, "GET /snapshot")
            current = row(
                snapshot, budgeted["id"], unbudgeted["id"], active)
            if current["budgeted"] == "good" \
                    and current["backup_active"] is False:
                recovered = current
                break
        evidence["recovered"] = recovered
        if recovered is None:
            last = row(
                http(f"{standby_url}/snapshot", None, "GET /snapshot"),
                budgeted["id"], unbudgeted["id"], active)
            raise Inconclusive(
                "the resumed writer never returned the budgeted point to "
                "Good inside the recovery bound — the launched tooling "
                "predates the recovery the declared budget owes, or the "
                "field never resumed stepping",
                f"the last served row was {last}")
        for _ in range(SETTLE_SCANS):
            _tracked, owner_snapshot = rig.tick(
                standby_url, duty_url, failures,
                diverged="the pair's images diverged at tick {tick} — "
                "the writer's recovery was not bumpless",
            )
            handover.append(owner_snapshot["tick"])
        digest_entries.append(
            {
                "phase": "recover",
                "recovered": recovered,
                "ticks": handover,
                "annunciation": "silent",
            }
        )

        # Phase 4 — the durable evidence: the surviving peer's served
        # history for the budgeted point retains the stale interval
        # between healthy samples.
        history = stale_history(standby_url, budgeted["id"])
        evidence["history"] = history
        words = [word for _seq, word in history]
        stale_positions = [
            index for index, word in enumerate(words)
            if word == "uncertain:stale"
        ]
        if not stale_positions:
            failures.append(
                f"the surviving peer's history for the budgeted point "
                f"({budgeted['id']}) retains no stale sample — the "
                "declared budget's own evidence did not survive in the "
                f"record: {history}"
            )
            raise Abort
        healed = next(
            (index for index in stale_positions
             if any(word == "good" for word in words[index + 1:])),
            None,
        )
        if healed is None:
            failures.append(
                "the retained history ends inside the stale interval — "
                "the recovery never reached the recorded series: "
                f"{history}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "history",
                "samples": len(history),
                "stale": len(stale_positions),
                "healed": healed,
            }
        )

        # Phase 5 — the pair's launch roles restored: the field owner
        # active with the declared standby tracking it.
        if not driver_recovery.roles_hold(
            rig, failures, "after the writer-loss recovery"
        ):
            raise Abort
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
        choices=["expect-last-good"],
        help="doctor the stage's expectation to the defect shape — the "
        "held read served as a healthy last-known value — so the pass "
        "must fail naming the degraded presentation it observed",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = stale_freshness_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"stale-freshness: {TAMPER_EVIDENCE} — an inconclusive "
                "run offers the doctored case no evidence"
            )
            return 1
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"stale-freshness: inconclusive — {detail}")
        print(f"stale-freshness-digest inconclusive — {reason}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"stale-freshness: {line}")
        return 1
    for failure in failures:
        eprint(f"stale-freshness: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"stale-freshness: the {args.tamper} case passed "
                "silently — the stage never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"stale-freshness-digest {digest} — the declared budget of "
        f"{evidence['surface']['budget']} on point "
        f"{evidence['surface']['budgeted']} presented stale "
        f"{evidence['lag']} served reads into the writer-loss window "
        f"while its unbudgeted neighbour "
        f"{evidence['surface']['unbudgeted']} kept reading good, the "
        f"failover-select annunciated, and the resumed writer returned "
        f"the point to good with the stale interval retained in "
        f"{evidence['history'][-1][0] and len(evidence['history'])} "
        "recorded samples — the pair's launch roles stand"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())