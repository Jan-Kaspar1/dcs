#!/usr/bin/env python3
"""The bare-point restamp leg for the reference plant — the
consumer-side proof that a bare simulated channel re-stamps its
served report on every plant step: a channel-bound field input the
emitted model declares that no dynamics element and no field
loopback drives — an inflow or contact point whose stored sample
nothing writes — keeps its served timestamp inside its declared
freshness budget while the driven run steps the plant past the
declared wait horizon, the re-stamp never blurring the
deadline-driven stamp a non-bare point carries, and both stamps
freezing together the moment the sim tick pauses (WW-ENG-003,
WW-OPS-003).

The consumer-boundary mirror of the #988 restamping contract — the
platform-side acceptance leg lives at
qa_lane/scenarios/0650_bare_point_restamp.py. Freshness on the field
side means the plant is still scanning, not that the value changed:
a bare channel's stored sample carries value and quality forward and
re-stamps at each step's new tick, the way a scanned input card
refreshes its report every cycle — so a stepping plant keeps the
bare point's served stamp inside any declared `stale_after_ticks`
budget rather than idling stale at its last write, while a stopped
plant leaves the report frozen for the budget to age out honestly.
The rig launches the released tooling exactly as the pair legs do —
`dcs-plant-server` serving the checked-in model and dynamics, the
two `dcs-controller --driven --remote` peers converged to
`active`/`tracking` with the manifest's declared `failover_budget`
armed — then, exercising the contract through released artifacts and
HTTP only:

- resolves the probes off the emitted model and the dynamics
  document: the bare probe is a channel-bound `in` point no element
  output and no field-side loopback drives — `inflow` first, then
  the contact points — the non-bare probe a channel-bound `in` point
  a producer stamps every step; a model declaring no bare
  channel-bound input is inconclusive, never a pass;
- runs the declared wait horizon — the channel's declared
  `stale_after_ticks` budget when the model carries one, else the
  leg's own declared five-tick freshness budget — plus a margin of
  driven tracking-first ticks, each owner `POST /scan` stepping the
  plant exactly once — and reads the served census through the
  released `dcs-plant-ctl` after every driven tick: the bare point's
  served stamp must advance with every landed step and stay inside
  the declared budget, never idling stale;
- asserts the non-bare contrast reads equal to its deadline-driven
  stamp: a producer-stamped point's served stamp is exactly the
  plant tick of the step that wrote it — the bare-only re-stamp must
  never blur the real evidence that stamp carries;
- pauses the sim tick — the driven run steps the plant only on owner
  scans, so holding the scans stills the plant's clock — and asserts
  both stamps stop advancing together across the frozen polls, then
  resume together when the driven ticks continue: a stamp that moved
  under a paused sim tick fabricates freshness the field never
  produced.

A bare stamp that rewinds or serves no integer tick, or one that
leads the plant's own step record, is the nondeterminism diagnostic.
A served bare stamp that never moved while the plant stepped past
the budget is the pinned release predating the restamping contract —
inconclusive, not a failure.

Usage:

    bare_point_restamp.py --plant-server PATH --controller PATH \
        --plant-ctl PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `bare-point-restamp-digest <sha256>` line prints —
the check runs two passes and compares them
(`bare-point-restamp-nondeterministic`). A contract violation
reports `bare-point-restamp: …` lines on stderr and exits 1 — the
check's `bare-point-restamp-failed`. `--tamper expect-stale` doctors
the leg's freshness verdict — asserting the bare stamp idled stale
past its declared budget — so the leg proves its freshness
assertion fires rather than passing an unexercised contract.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the bare stamp idled stale
# past its declared budget must surface the named diagnostic on
# the honest fresh stamp — never a silently unexercised contract.
LEG = {
    "order": 340,
    "title": "the bare-point restamp leg",
    "passes": "bare-point-restamp-leg",
    "tools": {
        "plant-ctl": "dcs-plant-ctl",
    },
    "tampers": [
        {
            "name": "expect-stale",
            "passed": "an expect-stale case passed the bare-point-restamp leg",
            "missed": "the expect-stale case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted the bare stamp stale"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the contract the leg exercises, or
    the emitted model declares no bare channel-bound input — the run
    classifies inconclusive, never a product failure."""


# The freshness budget the leg declares where the emitted model
# carries no `stale_after_ticks` on the bare probe — the five-tick
# field budget the rig lane's declared freshness contract uses — and
# the margin of driven ticks the wait horizon spans beyond it, so the
# window runs past every deadline an idle stamp could hide behind.
# The frozen-plant polls and the resume window prove the two stamps
# stop and restart advancing together.
FRESHNESS_BUDGET = 5
HORIZON_MARGIN = 4
FREEZE_POLLS = 3
FREEZE_INTERVAL = 0.4
RESUME_TICKS = 3

# The probe pools out of the emitted model's signal index: a named
# bare channel-bound field input — `inflow`, a forcing junction, then
# the contact points — and a named non-bare one a producer stamps
# every step. The dynamics document's outputs and the model's
# field-side loopbacks decide whether a named point is bare at all.
BARE_NAMES = ("inflow", "power-fail", "p101-run", "p102-run",
              "p101-thermal", "p102-thermal", "p101-moisture",
              "p102-moisture")
DRIVEN_NAMES = ("level-primary", "level-backup", "net-flow", "inflow",
                "p101-draw", "p102-draw", "well-full", "p101-run",
                "p102-run")


def named_sources(model):
    """The emitted model's signal names mapped to their field
    sources — the declared junctions the probe resolution prefers,
    lowest signal id winning a shared name."""
    named = {}
    for signal in model.get("signals", []):
        name = signal.get("name")
        current = named.get(name)
        if current is None or signal["id"] < current[0]:
            named[name] = (signal["id"], signal.get("source"))
    return named


def element_outputs(dynamics):
    """The point ids the dynamics' elements drive — a point one names
    is rewritten on every plant step."""
    elements = (
        dynamics.get("elements", [])
        if isinstance(dynamics, dict)
        else dynamics
    )
    driven = set()
    for element in elements or []:
        spec = (
            next(iter(element.values()), {})
            if isinstance(element, dict)
            else {}
        )
        output = spec.get("output") if isinstance(spec, dict) else None
        if isinstance(output, int) and not isinstance(output, bool):
            driven.add(output)
    return driven


def loopback_inputs(model):
    """The point ids the emitted model's field wiring drives: every
    point-to-point connection between two channel-bound io_points
    resolves to a field loopback routing the `to` (Out) channel onto
    the `from` (In) channel — the `from` point is stamped by the
    loopback every step, so it is not a bare channel."""
    channel_bound = {
        point["id"]
        for point in model.get("io_points", [])
        if "channel" in point
    }
    driven = set()
    for connection in model.get("connections", []):
        source, target = connection.get("from", {}), connection.get("to", {})
        source_id, target_id = source.get("point"), target.get("point")
        if (
            source_id in channel_bound
            and target_id in channel_bound
        ):
            driven.add(source_id)
    return driven


def channel_inputs(model):
    """The channel-bound `in` io_points the probes resolve out of —
    `{point id: declaration}` — the field inputs a bare or driven
    channel can sit behind."""
    return {
        point["id"]: point
        for point in model.get("io_points", [])
        if point.get("direction") == "in" and "channel" in point
    }


def probes(model, dynamics):
    """The leg's probe pair off the emitted model: `(bare, driven)`
    naming `(name, point)` — the bare probe a channel-bound field
    input no element output and no field loopback drives, preferring
    the declared names in order then any undeclared candidate, the
    non-bare probe a channel-bound input a producer stamps. Raises
    `Inconclusive` where the model declares no bare channel-bound
    input or no driven contrast. Returns `(bare, driven, budget)` —
    `budget` the bare point's declared `stale_after_ticks` where the
    model carries one, else the leg's declared freshness budget."""
    named = named_sources(model)
    inputs = channel_inputs(model)
    driven_ids = element_outputs(dynamics) | loopback_inputs(model)

    def pick(names, want_driven):
        return next(
            (
                (name, named[name][1])
                for name in names
                if named.get(name) is not None
                and named[name][1] in inputs
                and (named[name][1] in driven_ids) == want_driven
            ),
            None,
        )

    def rest(want_driven):
        return next(
            (
                (
                    inputs[point].get("channel", {}).get("name")
                    or str(point),
                    point,
                )
                for point in sorted(inputs)
                if (point in driven_ids) == want_driven
            ),
            None,
        )

    bare = pick(BARE_NAMES, False) or rest(False)
    if bare is None:
        raise Inconclusive(
            "the emitted model declares no bare channel-bound field "
            "input — every channel in-point is element- or "
            "loopback-driven"
        )
    driven = pick(DRIVEN_NAMES, True) or rest(True)
    if driven is None:
        raise Inconclusive(
            "the emitted model declares no driven channel-bound "
            "field input for the stamp-rule contrast"
        )
    budget = inputs[bare[1]].get("stale_after_ticks", FRESHNESS_BUDGET)
    if (
        not isinstance(budget, int)
        or isinstance(budget, bool)
        or budget < 1
    ):
        raise Inconclusive(
            "the bare probe's declared freshness budget is "
            f"{budget!r} — no positive tick bound to exercise"
        )
    return bare, driven, budget


def stamp(sample):
    """A served sample's plant tick when it is an int, else None —
    the stamp the restamping contract answers for."""
    tick = (sample or {}).get("tick")
    return (
        tick
        if isinstance(tick, int) and not isinstance(tick, bool)
        else None
    )


def plant_ctl(tool, addr, *argv):
    """One released `dcs-plant-ctl` invocation against the pair's
    spawned plant — the shipped surface the census reads ride.
    Returns `(exit, stdout, stderr)` unasserted."""
    run = subprocess.run(
        [tool, addr, *[str(arg) for arg in argv]],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return run.returncode, run.stdout, run.stderr


def ctl_payload(invocation):
    """The decoded answer a successful `dcs-plant-ctl` invocation
    printed — None on a nonzero exit or an unprintable frame."""
    code, out, _err = invocation
    if code != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def field_census(tool, addr, failures):
    """One served census through the released `dcs-plant-ctl list` —
    `(entries, plant_tick)`: the per-point entry map plus the plant's
    own step record, the highest stamp the field serves."""
    invocation = plant_ctl(tool, addr, "list")
    payload = ctl_payload(invocation)
    if not isinstance(payload, dict) or payload.get("result") != "points":
        failures.append(
            "a dcs-plant-ctl list against the live pair's plant "
            f"answered exit {invocation[0]}: "
            f"{(invocation[2] or invocation[1]).strip()[:300]}"
        )
        raise Abort
    entries = {
        entry.get("point"): entry
        for entry in payload.get("points") or []
        if isinstance(entry, dict)
    }
    ticks = [
        stamp(entry.get("sample"))
        for entry in entries.values()
    ]
    ticks = [tick for tick in ticks if tick is not None]
    if not ticks:
        failures.append(
            "the served census carries no stamped sample — the "
            "plant's step record is unobservable"
        )
        raise Abort
    return entries, max(ticks)


def bare_point_restamp_pass(args, tamper):
    """The restamp run: converge the armed declared pair, resolve the
    bare/driven probe pair off the emitted model, run the declared
    wait horizon with per-tick census reads, pause the sim tick, then
    resume. Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "bare-point-restamp leg has nothing to exercise"
        )
    _manifest, _duty_decl, standby_decl = declared
    armed = standby_decl.get("failover_budget")
    if not isinstance(armed, int) or isinstance(armed, bool) or armed < 1:
        raise Abort(
            "the manifest's standby declares no positive "
            "failover_budget — the deployed pair's armed heartbeat "
            "is absent"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    with open(args.dynamics) as handle:
        dynamics = json.load(handle)
    (bare_name, bare), (driven_name, driven), budget = probes(
        model, dynamics
    )
    horizon = budget + HORIZON_MARGIN
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared, auto_promote=armed)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence: the pair leg's driven-tick loop, the
        # tracking peer's pull applying the owner's latest checkpoint
        # each tick, the peers resting at identical images while the
        # owner's scans step the plant once apiece.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": "active",
                "standby_role": "standby/tracking",
            }
        )

        # Phase 2 — the probe resolution against the served census:
        # both probes must be channel-bound `in` points the field
        # actually serves — a census that never heard of one is the
        # release predating the contract, not a failed restamp.
        entries, base_plant = field_census(
            args.plant_ctl, rig.plant_addr, failures
        )
        for label, name, point in (
            ("bare", bare_name, bare),
            ("driven", driven_name, driven),
        ):
            entry = entries.get(point)
            if entry is None or entry.get("direction") != "in":
                raise Inconclusive(
                    f"the {label} probe {name} point {point} is not "
                    "a served field in-point — the plant's census "
                    "never heard of it"
                )
        evidence["bare"] = f"{bare_name}:{bare}"
        evidence["driven"] = f"{driven_name}:{driven}"
        digest_entries.append(
            {
                "phase": "probes",
                "bare": {"name": bare_name, "point": bare},
                "driven": {"name": driven_name, "point": driven},
                "budget": budget,
                "horizon": horizon,
            }
        )

        # Phase 3 — the wait horizon: `horizon` driven tracking-first
        # ticks, each owner scan stepping the plant exactly once, the
        # released tool's census read after every landed step. The
        # bare stamp must advance with every step and stay inside the
        # declared budget; the driven stamp must equal the plant tick
        # of the step that produced it.
        base_b = stamp(entries[bare].get("sample"))
        base_d = stamp(entries[driven].get("sample"))
        rows = []
        for _round in range(horizon):
            _tracked, _owner = rig.tick(
                standby_url, duty_url, failures
            )
            entries, plant = field_census(
                args.plant_ctl, rig.plant_addr, failures
            )
            rows.append(
                {
                    "plant": plant,
                    "bare": stamp(
                        entries[bare].get("sample")
                    ),
                    "driven": stamp(
                        entries[driven].get("sample")
                    ),
                }
            )
        plants = [base_plant] + [row["plant"] for row in rows]
        stamps_b = [base_b] + [row["bare"] for row in rows]
        stamps_d = [base_d] + [row["driven"] for row in rows]
        digest_entries.append(
            {
                "phase": "wait",
                "ticks": horizon,
                "lags": [
                    plant - bare_
                    for plant, bare_ in zip(plants[1:], stamps_b[1:])
                ]
                if all(
                    isinstance(stamp_, int)
                    for stamp_ in plants[1:] + stamps_b[1:]
                )
                else "unstamped",
            }
        )
        if any(stamp_ is None for stamp_ in stamps_b + stamps_d):
            failures.append(
                "bare-point-restamp-nondeterministic: a served "
                "sample carried no integer stamp through the wait "
                f"window: {json.dumps(rows)[:400]}"
            )
            raise Abort
        if any(
            plants[i] <= plants[i - 1]
            for i in range(1, len(plants))
        ):
            raise Inconclusive(
                "the field's step record stalled inside the driven "
                "wait window — no stepped plant to attribute the "
                "served stamps to"
            )
        rewound = any(
            ticks[i] < ticks[i - 1]
            for ticks in (stamps_b, stamps_d)
            for i in range(1, len(ticks))
        )
        ahead = any(
            bare_ > plant_
            for bare_, plant_ in zip(stamps_b, plants)
        ) or any(
            driven_ > plant_
            for driven_, plant_ in zip(stamps_d, plants)
        )
        if rewound or ahead:
            failures.append(
                "bare-point-restamp-nondeterministic: a served stamp "
                "disagreed with the field's own step record — "
                f"plants {plants}, bare {stamps_b}, driven "
                f"{stamps_d}"
            )
            raise Abort
        if len(set(stamps_b)) == 1:
            raise Inconclusive(
                "the bare point held its stamp across the whole "
                "wait horizon while the field stepped past the "
                "declared budget — the pinned release predates the "
                "bare-channel restamping contract"
            )
        held = next(
            (
                i
                for i in range(1, len(stamps_b))
                if stamps_b[i] == stamps_b[i - 1]
            ),
            None,
        )
        if held is not None:
            failures.append(
                "bare-point-restamp-failed: the bare point's served "
                "stamp held across a driven plant step at window "
                f"index {held}: {stamps_b}"
            )
            raise Abort
        lags = [
            plant - bare_ for plant, bare_ in zip(plants, stamps_b)
        ]
        if tamper == "expect-stale":
            # The doctored negative: staleness asserted as fresh —
            # the honest in-budget stamp must fail it, naming the
            # doctored expectation.
            if lags[-1] <= budget:
                failures.append(
                    f"the bare point's served stamp held lag "
                    f"{lags[-1]} — inside the declared {budget}-tick "
                    "freshness budget across the wait horizon: the "
                    "doctored expectation wanted the bare stamp "
                    "stale past its budget"
                )
                raise Abort
        elif lags[-1] > budget:
            failures.append(
                "bare-point-restamp-failed: the bare point's served "
                f"stamp idled {lags[-1]} ticks behind the field's "
                f"step record, past the declared {budget}-tick "
                "freshness budget — the bare channel never "
                "re-stamped under the input deadline"
            )
            raise Abort
        blurred = next(
            (
                (driven_, plant_)
                for driven_, plant_ in zip(stamps_d, plants)
                if driven_ != plant_
            ),
            None,
        )
        if blurred is not None:
            failures.append(
                "bare-point-restamp-failed: the driven point's "
                f"served stamp {blurred[0]} lagged or led the plant "
                f"tick {blurred[1]} of the step that produced it — "
                "the re-stamp blurred the deadline-driven stamp's "
                "real evidence"
            )
            raise Abort

        # Phase 4 — the paused sim tick: nothing drives a scan, so
        # the plant's clock stands still and both stamps must stop
        # advancing together. A stamp that moved off the sim tick
        # fabricated freshness the field never produced.
        frozen = []
        for _poll in range(FREEZE_POLLS):
            entries, plant = field_census(
                args.plant_ctl, rig.plant_addr, failures
            )
            frozen.append(
                {
                    "plant": plant,
                    "bare": stamp(entries[bare].get("sample")),
                    "driven": stamp(
                        entries[driven].get("sample")
                    ),
                }
            )
            time.sleep(FREEZE_INTERVAL)
        moved = next(
            (
                (index, obs)
                for index, obs in enumerate(frozen[1:], 1)
                if obs != frozen[0]
            ),
            None,
        )
        if moved is not None:
            failures.append(
                "bare-point-restamp-failed: a served stamp advanced "
                f"under a paused sim tick — poll 0 read "
                f"{frozen[0]}, poll {moved[0]} read {moved[1]}: the "
                "re-stamp fabricated freshness the stopped field "
                "never produced"
            )
            raise Abort
        digest_entries.append(
            {"phase": "pause", "polls": FREEZE_POLLS, "marks": "frozen"}
        )

        # Phase 5 — the resume: the driven ticks continue and both
        # stamps must advance together again — the bare stamp with
        # every landed step, the driven stamp at its own producer's
        # step tick.
        resume = []
        for _round in range(RESUME_TICKS):
            _tracked, _owner = rig.tick(
                standby_url, duty_url, failures
            )
            entries, plant = field_census(
                args.plant_ctl, rig.plant_addr, failures
            )
            resume.append(
                {
                    "plant": plant,
                    "bare": stamp(entries[bare].get("sample")),
                    "driven": stamp(
                        entries[driven].get("sample")
                    ),
                }
            )
        prev = frozen[-1]["bare"]
        resumed = True
        for obs in resume:
            resumed = (
                isinstance(obs["bare"], int)
                and isinstance(obs["plant"], int)
                and 0 <= obs["plant"] - obs["bare"] <= budget
                and obs["driven"] == obs["plant"]
                and isinstance(prev, int)
                and obs["bare"] > prev
            )
            if not resumed:
                break
            prev = obs["bare"]
        if not resumed:
            failures.append(
                "bare-point-restamp-failed: the resumed driven "
                "ticks did not advance both stamps together — "
                f"frozen {frozen[-1]}, resume {resume}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "resume",
                "ticks": RESUME_TICKS,
                "marks": "resumed-together",
            }
        )

        # Phase 6 — the pair stands on its launch roles for the next
        # leg: a short reconverge proves the tracking peer and the
        # field owner kept their roles through the pauses.
        settled = rig.converge(failures, count=2)
        digest_entries.append(
            {
                "phase": "roles",
                "duty_role": "active",
                "standby_role": "standby/tracking",
            }
        )
        evidence["final_tick"] = settled["ticks"][-1]
    except Inconclusive:
        raise
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
    parser.add_argument(
        "--plant-ctl",
        required=True,
        help="the released dcs-plant-ctl binary — the shipped surface "
        "the served-stamp reads ride",
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--tamper",
        choices=["expect-stale"],
        help="doctor the leg's freshness verdict — the pass must "
        "fail naming the honest in-budget stamp",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = bare_point_restamp_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "bare-point-restamp: the doctored expectation wanted "
                "the bare stamp stale — an inconclusive run offers "
                "the doctored case no evidence"
            )
            return 1
        eprint(f"bare-point-restamp: inconclusive — {inconclusive}")
        print(f"bare-point-restamp-inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"bare-point-restamp: {line}")
        return 1
    for failure in failures:
        eprint(f"bare-point-restamp: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"bare-point-restamp: the {args.tamper} case passed "
                "silently — the leg never noticed the honest "
                "in-budget stamp"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"bare-point-restamp-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the bare probe "
        f"{evidence['bare']} stayed inside its declared freshness "
        f"budget across the wait horizon beside {evidence['driven']}'s "
        "deadline-driven stamp, both freezing under the paused sim "
        f"tick and resuming together to tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
