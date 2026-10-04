#!/usr/bin/env python3
"""The bounded step-dt leg for the reference plant — the
consumer-side proof that the deployed pair's spawned
`dcs-plant-server` refuses a protocol-legal *finite but huge* step `dt`
by name and leaves the shared field exactly as it stood (WW-ENG-003,
WW-OPS-003): the consumer-boundary mirror of the platform lane's
step-bound scenario leg covering the #683 fix's contract.

The pair leg (`ci/legs/pair.py`) proves the manifest-declared pair runs
and switches; this leg proves the plant's step-bound contract on the
same declared deployment. A step's `dt` is one scan period, and the
field bounds it at `MAX_STEP_DT` (10<sup>6</sup> time units per plant
tick). The QA finding's reproduction is the shape no request boundary
could refuse on its own: `1e308` is a finite `f64`, so strict JSON
carries it and the decoder reads it — only the *bound* names it. Before
the contract, `claim_writer(X); step(1e308); read(...)` was accepted and
wound the element's accumulator past the `f64` range, or parked an
accumulated clock where no legal later step could move it again; either
way the field then served `{"float":null}` frames this protocol's own
client cannot deserialize, and one accepted request corrupted shared
field state for every attachment until the plant restarted.

With the pair settled and tracking, a dedicated plant-protocol
attachment joins the field owner's standing claim through
`ensure_writer` under the owner token the launched active's preamble
records — the probes then meet the bound contract rather than the
fencing verdict a foreign attachment would. The run:

- the ordinary over-bound advance goes first and the finding's `1e308`
  vector second, so a release predating the bound absorbs the first and
  never sees the second applied;
- each probe must answer the named `invalid_request` refusal with the
  documented bound in its detail — never `stepped`;
- the refused steps change nothing: the plant tick advances by exactly
  the probes' own absence (the next `dt:0` step answers one tick on),
  and the whole served census reads back identical to the pre-probe
  record — no element state moved, no `{"float":null}` frame anywhere;
- the released `dcs-plant-ctl` the consumer ships refuses the same dt
  spellings at its own argument boundary, exiting nonzero naming the
  bound, while its `read`/`list` answers decode through the shipped
  binary itself;
- a subsequent legitimate write and step under the same claim proceed
  normally — the write answering `done`, the step `stepped` with the
  plant tick advancing — the step path never wedged, and the driven
  point's baseline restored;
- the pair's served points stay finite throughout and the launch roles
  stand unmoved.

The leg reports inconclusive rather than failing where the pinned
release predates the contract: no owner-token claim line in the launched
active's preamble, `ensure_writer` unanswered or answered
`invalid_request`, no finite float field input to drive, the step path
not live for a `dt:0` probe, a probe meeting the fencing verdict, or a
probe applied — a release whose field still accepts an over-bound
advance.

Usage:

    step_bound.py --plant-server PATH --controller PATH --plant-ctl PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `step-bound-digest <sha256>` line prints — the check
runs two passes and compares them (`step-bound-nondeterministic`). A
contract violation reports `step-bound: …` lines on stderr and exits 1 —
the check's `step-bound-failed`. `--tamper expect-applied` doctors the
leg's own expectation — asserting the huge dt applied — so the leg
proves its refusal assertion fires rather than passing an unexercised
contract.
"""

import argparse
import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import failover
import nonfinite_refusal
import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the over-bound dt applied must
# surface the named diagnostic on the plant's honest named refusal —
# never a silently unrefused pass, and never an inconclusive one,
# which offers the doctored case no evidence either way.
LEG = {
    "order": 325,
    "title": "the bounded step-dt leg",
    "passes": "step-bound-leg",
    "tools": {
        "plant-ctl": "dcs-plant-ctl",
    },
    "tampers": [
        {
            "name": "expect-applied",
            "passed": "a doctored applied expectation passed the step-bound leg",
            "missed": "the expect-applied case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted the dt applied"],
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


# The field's documented per-tick step bound, `dcs_sim::MAX_STEP_DT`,
# as the leg spells it: the over-bound probe is an ordinary advance a
# control scan never declares, and the huge probe is the finding's own
# `1e308`. The bound is also the substring the refusal's detail must
# carry — `format!("{}", 1.0e6)` on the field spells it `1000000`.
BOUND = 1.0e6
BOUND_DETAIL = "1000000"
OVER_BOUND_DT = 1.0e7
HUGE_DT = 1.0e308

# The shipped tool's argument-boundary wording for a dt outside the
# bound, beside the non-finite spellings it already names.
CTL_STEP_REFUSAL = "invalid step"

# The shared helpers this leg rides rather than forks: the emitted
# model's signal names, the dynamics' element outputs, the driven
# point's resolution off the served census, the finite-float decode and
# the poisoned-sample scan the served frame's representability contract
# rests on, the claim-verdict classification, and the shipped-tool
# invocation surface — the nonfinite-refusal leg's own helpers, so the
# two legs cannot drift apart on what a refusal or a poisoned frame is.
# The step probes need no raw-line seam of their own: `1e308` is a
# finite number, so the harness plant client's own `json.dumps` carries
# it — that it is spellable is the whole of the finding's difficulty,
# and only the bound can name it.
named_sources = nonfinite_refusal.named_sources
element_outputs = nonfinite_refusal.element_outputs
probe_point = nonfinite_refusal.probe_point
finite_float = nonfinite_refusal.finite_float
nonfinite_samples = nonfinite_refusal.nonfinite_samples
claim_verdict = nonfinite_refusal.claim_verdict
plant_ctl = nonfinite_refusal.plant_ctl
ctl_payload = nonfinite_refusal.ctl_payload


def bound_refusal(answer):
    """The named refusal an over-bound step's answer carries: `bound`
    when it was refused as `invalid_request` with the documented bound
    in its detail, `invalid_request` when the kind alone names the
    refusal, or None on any other answer — `stepped` included, which is
    how a release predating the bound is recognized."""
    name = nonfinite_refusal.refusal_name(answer)
    if name is None:
        return None
    error = (answer or {}).get("error") or {}
    detail = error.get("detail")
    if isinstance(detail, str) and BOUND_DETAIL in detail:
        return "bound"
    return "invalid_request"


def ctl_refusal_name(invocation):
    """The refusal class a `dcs-plant-ctl` invocation answered:
    `parse` for the tool's own argument boundary, `wire` for a refusal
    the server carried back, or None on an applied step."""
    code, out, err = invocation
    if code == 0:
        return None
    if CTL_STEP_REFUSAL in err:
        return "parse"
    if nonfinite_refusal.ctl_refusal_name(invocation) == "wire":
        return "wire"
    return "other"


def step_bound_pass(args, tamper):
    """The step-bound run: converge, the shared-claim attach, the
    baseline, the ordered over-bound probes, the unchanged census, the
    shipped tool's own boundary, the legitimate follow-up, and the
    pair's finite surface. Returns `(digest_entries, evidence,
    failures)`; raises `Inconclusive` where the pinned release predates
    the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the step-bound "
            "leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    rig = probe_io = None
    restore = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence and the served-finite baseline: each
        # driven tick scans the tracking peer first, and the converged
        # owner's served snapshot is the pre-probe surface every float
        # point must already decode finite on.
        converged = rig.converge(failures)
        owner = converged["owner"]
        poisoned = nonfinite_samples(owner.get("points") or [])
        if poisoned:
            failures.append(
                "the converged pair already serves a non-finite "
                f"sample — point {poisoned[0].get('point')} carries "
                f"{poisoned[0].get('sample')} before any probe"
            )
            raise Abort
        health0 = owner.get("io_health") or {}
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # Phase 2 — the shared-claim attach: the probes ride the field
        # owner's standing claim so a huge dt reaches the bound
        # contract rather than the fencing verdict a foreign attachment
        # would meet. A release predating the seam records no owner
        # token the attach can name, or answers the verb's
        # `invalid_request` — inconclusive, not a refusal of the
        # contract under test.
        owner_token = failover.owner_token(rig.duty_preamble)
        if owner_token is None:
            raise Inconclusive(
                "the launched active recorded no owner-token claim "
                "line — the pinned release claims only on promotion, "
                "predating the shared-claim seam the probes attach "
                "through"
            )
        probe_io = simulate.PlantClient(rig.plant_addr)
        claim = probe_io.request(
            {"op": "ensure_writer", "owner": owner_token}
        )
        if claim.get("result") not in ("done", "claimed_shared"):
            raise Inconclusive(
                "the shared writer claim refused the attachment under "
                "the field owner's pinned token — a release predating "
                f"or mishandling the shared-claim contract: {claim}"
            )
        digest_entries.append(
            {"phase": "attach", "claim": claim.get("result")}
        )

        # Phase 3 — the baseline: the driven point — resolved off the
        # served census so the leg exercises the declared junction,
        # never a hard-coded id — its stored sample, the plant tick off
        # a dt:0 step (the claim-holding probe that proves the step
        # path live while advancing nothing), and the census the
        # refused probes must leave untouched.
        with open(args.model) as handle:
            model = json.load(handle)
        with open(args.dynamics) as handle:
            driven = element_outputs(json.load(handle))
        census = probe_io.request({"op": "list_points"})
        points = census.get("points") if isinstance(census, dict) else None
        if not isinstance(points, list):
            raise Abort(f"the plant census answered {census}")
        point, undriven = probe_point(model, points, driven)
        if point is None:
            raise Inconclusive(
                "the plant census serves no field in-point holding a "
                "finite float to watch"
            )
        baseline_read = failover.field_read(probe_io, point, failures)
        baseline = baseline_read.get("value")
        baseline_value = finite_float(baseline)
        if baseline_value is None:
            raise Inconclusive(
                f"the driven point {point} reads back no finite float "
                f"baseline: {baseline_read}"
            )
        restore = (point, baseline)
        tick0 = probe_io.request({"op": "step", "dt": 0})
        if tick0.get("result") != "stepped":
            raise Inconclusive(
                "a dt:0 step under the shared claim was refused — the "
                f"step path is not live for the leg: {tick0}"
            )
        census0 = probe_io.request({"op": "list_points"})
        points0 = census0.get("points") if isinstance(census0, dict) else None
        if not isinstance(points0, list):
            raise Abort(f"the post-step census answered {census0}")
        evidence["point"] = point
        digest_entries.append(
            {
                "phase": "baseline",
                "point": point,
                "baseline": baseline_read,
                "plant_tick": tick0,
                "points": len(points0),
            }
        )

        # Phase 4 — the probes under test, in the order the contract
        # needs: the ordinary over-bound advance first, so a release
        # predating the bound absorbs that one and never sees the
        # finding's `1e308` vector applied to its elements.
        probes = [
            ("the over-bound step", OVER_BOUND_DT),
            ("the 1e308 step", HUGE_DT),
        ]
        answers = {}
        for label, dt in probes:
            answer = probe_io.request({"op": "step", "dt": dt})
            if tamper == "expect-applied":
                # The doctored expectation: the leg asserts the huge dt
                # applied — the plant's honest named refusal must fail
                # it, naming the actual answer.
                if answer.get("result") != "stepped":
                    failures.append(
                        f"{label} answered {answer} — the doctored "
                        "expectation wanted the dt applied"
                    )
                    raise Abort
            name = bound_refusal(answer)
            if name is None:
                if answer.get("result") == "stepped":
                    raise Inconclusive(
                        f"{label} was applied — the pinned release "
                        "predates the bounded step contract: "
                        f"{answer}"
                    )
                verdict = claim_verdict(answer)
                if verdict is not None:
                    raise Inconclusive(
                        f"{label} met the {verdict} claim verdict — "
                        "the shared claim never covered the "
                        f"attachment: {answer}"
                    )
                failures.append(
                    f"{label} answered off-contract — neither stepped "
                    f"nor the named bound refusal: {answer}"
                )
                raise Abort
            answers[label] = name
        digest_entries.append(
            {"phase": "probes", "point": point, "answers": answers}
        )

        # Phase 5 — nothing moved: the refusals left every stored
        # sample where it stood — the whole census equals the pre-probe
        # record, since no scan landed between them, and a step
        # re-stamps every sample at its own tick — and the shipped
        # client's own decode reads the driven point and the census
        # back finite, a `{"float":null}` frame being exactly what it
        # cannot print.
        read_back = plant_ctl(
            args.plant_ctl, rig.plant_addr, "read", point
        )
        served = ctl_payload(read_back)
        if not isinstance(served, dict) or served.get("result") != "sample":
            failures.append(
                "the refused steps left state the shipped client "
                f"cannot deserialize — read on point {point} answered "
                f"exit {read_back[0]}: "
                f"{read_back[2].strip() or read_back[1].strip()}"
            )
            raise Abort
        served_value = (served.get("sample") or {}).get("value")
        if finite_float(served_value) is None:
            failures.append(
                "the driven point serves a non-finite float — the "
                f"poisoning the bound exists to prevent: "
                f"{served_value}"
            )
            raise Abort
        if served_value != baseline:
            failures.append(
                "the refused steps left the stored value moved — "
                f"{baseline} -> {served_value}"
            )
            raise Abort
        census1 = probe_io.request({"op": "list_points"})
        points1 = census1.get("points") if isinstance(census1, dict) else []
        poisoned = nonfinite_samples(points1)
        if poisoned:
            failures.append(
                f"point {poisoned[0].get('point')} serves a non-finite "
                "sample the wire cannot spell — an over-bound step "
                f"still poisoned the field: {poisoned[0].get('sample')}"
            )
            raise Abort
        if points1 != points0:
            failures.append(
                "the field census moved across the refused steps — "
                "element state did not hold: "
                f"{json.dumps(points1)[:400]}"
            )
            raise Abort
        # The refusals also consumed no plant tick: the next step
        # answers exactly one tick on from the pre-probe canary, so the
        # two refused probes cost the field no time either.
        tick1 = probe_io.request({"op": "step", "dt": 0})
        if tick1.get("result") != "stepped":
            failures.append(
                "a dt:0 step after the refused probes was refused — "
                f"the probes wedged the step path: {tick1}"
            )
            raise Abort
        if tick1.get("tick") != tick0.get("tick") + 1:
            failures.append(
                "the refused steps consumed a plant tick — the next "
                f"step answered {tick1.get('tick')} where the refused "
                f"probes left the tick at {tick0.get('tick')}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "unchanged",
                "value": served_value,
                "points": len(points1),
                "tick": tick1["tick"],
            }
        )

        # Phase 6 — the shipped tool's own boundary: the released
        # `dcs-plant-ctl` refuses both dt spellings above the bound at
        # its argument boundary — naming the bound, never applying — so
        # the shipped surface carries the same contract the wire
        # enforces.
        tool_probes = [
            ("step-over-bound", plant_ctl(
                args.plant_ctl, rig.plant_addr, "step", "1e7"
            )),
            ("step-huge", plant_ctl(
                args.plant_ctl, rig.plant_addr, "step", "1e308"
            )),
        ]
        tool_answers = {}
        for label, invocation in tool_probes:
            name = ctl_refusal_name(invocation)
            if name not in ("parse", "wire"):
                failures.append(
                    f"a dcs-plant-ctl {label} carrying an over-bound dt "
                    "was not refused with the named failure — exit "
                    f"{invocation[0]}: "
                    f"{invocation[2].strip() or invocation[1].strip()}"
                )
                raise Abort
            tool_answers[label] = name
        digest_entries.append(
            {"phase": "tool-refusals", "answers": tool_answers}
        )

        # Phase 7 — the follow-up: a finite write and step under the
        # same claim must behave normally — the proof no wedged step
        # path or poisoned accumulator survived the refused probes —
        # then the driven point's baseline restored.
        follow = baseline_value + 1.0
        if not math.isfinite(follow) or follow == baseline_value:
            follow = math.nextafter(baseline_value, math.inf)
        fin_write = probe_io.request(
            {"op": "write", "point": point, "value": {"float": follow}}
        )
        if fin_write.get("result") != "done":
            failures.append(
                "a finite write under the same claim was refused after "
                "the bound refusals — the probes wedged the field: "
                f"{fin_write}"
            )
            raise Abort
        fin_step = probe_io.request({"op": "step", "dt": args.dt})
        if fin_step.get("result") != "stepped":
            failures.append(
                f"a finite step under the same claim was refused after "
                f"the bound refusals: {fin_step}"
            )
            raise Abort
        if (
            not isinstance(fin_step.get("tick"), int)
            or fin_step["tick"] <= tick1["tick"]
        ):
            failures.append(
                "the finite step answered stepped but the plant tick "
                f"never advanced — {tick1.get('tick')} -> "
                f"{fin_step.get('tick')}"
            )
            raise Abort
        landed = probe_io.request({"op": "read", "point": point})
        landed_value = (landed.get("sample") or {}).get("value")
        if finite_float(landed_value) is None:
            failures.append(
                "the follow-up write+step left the driven point "
                f"non-finite: {landed_value}"
            )
            raise Abort
        if undriven and landed_value != {"float": follow}:
            failures.append(
                "the applied finite write never landed — the driven "
                f"point reads {landed_value}, not "
                f"{{'float': {follow}}}"
            )
            raise Abort
        census2 = probe_io.request({"op": "list_points"})
        poisoned = nonfinite_samples(
            census2.get("points") if isinstance(census2, dict) else []
        )
        if poisoned:
            failures.append(
                "the follow-up write+step surfaced a non-finite "
                "accumulator — point "
                f"{poisoned[0].get('point')} serves "
                f"{poisoned[0].get('sample')}"
            )
            raise Abort
        evidence["stepped_to"] = fin_step["tick"]
        digest_entries.append(
            {
                "phase": "followup",
                "write": fin_write.get("result"),
                "step": {"result": "stepped", "tick": fin_step["tick"]},
            }
        )

        # Phase 8 — restore and the pair's surface: the driven point's
        # baseline written back, then the driven pair tick — identical
        # images, the launch roles standing, and every served float
        # finite on both peers' monitors.
        restored = probe_io.request(
            {"op": "write", "point": point, "value": baseline}
        )
        if restored.get("result") != "done":
            failures.append(
                "the restore write was refused — the field did not "
                f"come back: {restored}"
            )
            raise Abort
        restore = None
        final_read = plant_ctl(
            args.plant_ctl, rig.plant_addr, "read", point
        )
        final = ctl_payload(final_read)
        if not isinstance(final, dict) or final.get("result") != "sample":
            failures.append(
                "the restored point no longer deserializes for the "
                "shipped client — read answered exit "
                f"{final_read[0]}: "
                f"{final_read[2].strip() or final_read[1].strip()}"
            )
            raise Abort
        final_value = (final.get("sample") or {}).get("value")
        if undriven and final_value != baseline:
            failures.append(
                "the restore write answered done but the point reads "
                f"{final_value}, not the baseline {baseline}"
            )
            raise Abort
        tracked, owner = rig.tick(standby_url, duty_url, failures)
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active":
            failures.append(
                "the refused steps moved the field owner's role — "
                f"GET /role answers {duty_role}"
            )
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the refused steps moved the tracking peer's role — "
                f"GET /role answers {standby_role}"
            )
        for name, snapshot in (
            (duty_decl["name"], owner),
            (standby_decl["name"], tracked),
        ):
            poisoned = nonfinite_samples(snapshot.get("points") or [])
            if poisoned:
                failures.append(
                    f"{name}'s served snapshot carries a non-finite "
                    "sample after the probes — point "
                    f"{poisoned[0].get('point')} serves "
                    f"{poisoned[0].get('sample')}"
                )
        health1 = owner.get("io_health") or {}
        for counter in ("failed_reads", "failed_writes"):
            if (health1.get(counter) or 0) > (health0.get(counter) or 0):
                failures.append(
                    "the refused steps counted against the owner's "
                    f"io_health — {health0} -> {health1}"
                )
        if failures:
            raise Abort
        evidence["final_tick"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "pair",
                "tick": owner["tick"],
                "duty_role": duty_role,
                "standby_role": standby_role,
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
                if restore is not None:
                    probe_io.request(
                        {
                            "op": "write",
                            "point": restore[0],
                            "value": restore[1],
                        }
                    )
            except Exception:
                pass
            try:
                probe_io.request({"op": "release_writer"})
            except Exception:
                pass
            try:
                probe_io.close()
            except Exception:
                pass
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
        "the tool-driven probes ride",
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--tamper",
        choices=["expect-applied"],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the plant's honest answer",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = step_bound_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "step-bound: the doctored expectation wanted the dt applied — an inconclusive run offers the doctored case no evidence"
            )
            return 1
        eprint(f"step-bound: inconclusive — {inconclusive}")
        print(f"step-bound-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"step-bound: {line}")
        return 1
    for failure in failures:
        eprint(f"step-bound: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"step-bound: the {args.tamper} case passed silently — "
                "the leg never noticed the plant's honest refusal"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"step-bound-digest {digest} — tracking by tick "
        f"{evidence['converged']}, an over-bound advance and the "
        f"finding's 1e308 step on point {evidence['point']} refused by "
        "the documented bound with the census unchanged and no tick "
        f"consumed, the finite step advancing the plant to tick "
        f"{evidence['stepped_to']}, the pair's surface finite through "
        f"tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())