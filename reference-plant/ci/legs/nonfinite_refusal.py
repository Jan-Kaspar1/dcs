#!/usr/bin/env python3
"""The nonfinite write and step refusal leg for the reference plant —
the consumer-side proof that the deployed pair's spawned
`dcs-plant-server` refuses a protocol-legal non-finite payload by name
and never leaves non-finite element state behind for a later
legitimate step to propagate (WW-ENG-003, WW-FND-002): the
consumer-boundary mirror of the platform lane's nonfinite-refusal
scenario leg covering the #834/#866 input-validation contract.

The pair leg (`ci/legs/pair.py`) proves the manifest-declared pair runs
and switches; this leg proves the plant's value-boundary refusals on
the same declared deployment. A stored non-finite float serves
`{"float":null}` frames the protocol's own client cannot parse — the
field corrupting for every attachment until the plant restarts — so
the contract refuses the payload at its boundary rather than applying
it. `{"float":1e999}` is the spelling: strict JSON the wire carries
and a strict decoder reads as a non-finite f64 — the write dies at
the request boundary as `invalid_request`, with the point-level
`io.invalid_value` the other named refusal a deserialized non-finite
Value can meet, and a `"dt":1e999` or `"dt":-1` step dies the same
way — `invalid_request` is the only name a bad `dt` has.

With the pair settled and tracking, a dedicated plant-protocol
attachment joins the field owner's standing claim through
`ensure_writer` under the owner token the launched active's preamble
records — the probes then meet the value contract rather than the
fencing verdict a foreign attachment would. The run:

- the released `dcs-plant-ctl` the consumer ships drives the probes a
  tool can spell and the deserialization proof: `write` carrying a
  non-finite literal and `step` carrying a non-finite or out-of-range
  `dt` exit nonzero naming the refusal — the spellings the tool's own
  argument boundary or the field's fencing verdict answer, never an
  applied mutation — while `read`/`list` answers decode through the
  shipped binary itself, a `{"float":null}` frame being exactly what
  it cannot print;
- the raw write probes carry `{"float":1e999}` and
  `{"float":-1e999}` to the field's float junction, the raw step
  probes `"dt":1e999` and `"dt":-1` — each must answer the named
  refusal, never apply;
- the affected element state stays finite and unchanged: the driven
  point and the whole served census read back identical to the
  pre-probe record — the refused step never advanced the elements a
  poisoned write would have fed;
- a subsequent legitimate write and step under the same claim proceed
  normally — the write answering `done`, the step `stepped` with the
  plant tick advancing — the field unpoisoned by the refused
  payloads, and the driven point's baseline restored;
- the pair's served points stay finite throughout: the converged
  owner's snapshot ahead of the probes and both peers' images after
  them decode `{"float": <finite>}` on every float point, and the
  pair's launch roles stand unmoved.

The leg reports inconclusive rather than failing where the pinned
release predates the contract: no owner-token claim line in the
launched active's preamble, `ensure_writer` unanswered or answered
`invalid_request`, no finite float field input to drive, or a probe
meeting the fencing verdict instead of the value contract.

Usage:

    nonfinite_refusal.py --plant-server PATH --controller PATH \
        --plant-ctl PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `nonfinite-refusal-digest <sha256>` line prints — the
check runs two passes and compares them
(`nonfinite-refusal-nondeterministic`). A contract violation reports
`nonfinite-refusal: …` lines on stderr and exits 1 — the check's
`nonfinite-refusal-failed`. `--tamper expect-applied` doctors the
leg's own expectation — asserting the non-finite payloads apply — so
the leg proves its refusal assertion fires rather than passing an
unexercised contract.
"""

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import failover
import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the non-finite payloads apply
# must surface the named diagnostic on the plant's honest named
# refusal — never a silently unrefused pass.
LEG = {
    "order": 310,
    "title": "the nonfinite write and step refusal leg",
    "passes": "nonfinite-refusal-leg",
    "tools": {
        "plant-ctl": "dcs-plant-ctl",
    },
    "tampers": [
        {
            "name": "expect-applied",
            "passed": "a doctored applied expectation passed the nonfinite-refusal leg",
            "missed": "the expect-applied case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted the payload applied"],
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


# The field points the probe prefers, in order: `inflow` — the
# dynamics' forcing junction — then `net-flow`, the sum a poisoned
# write would feed the level integrators through — else any field
# in-point holding a finite float. Whether the resolved point is
# element-undriven comes off the dynamics doc — an element-driven
# point legitimately moves when the next plant step rewrites it, so
# the strict landed-equality checks apply only to a point no element
# outputs.
PREFERRED_POINTS = ("inflow", "net-flow")

# The substrings the shipped tool's argument boundary prints for a
# non-finite literal — `write`'s and `step`'s parse refusals ahead of
# any wire exchange.
CTL_VALUE_REFUSAL = "invalid value"
CTL_STEP_REFUSAL = "invalid step"


def raw_request(client, payload):
    """One plant-protocol round trip for a pre-encoded request line —
    `PlantClient.request` for the spellings `json.dumps` cannot emit:
    `1e999` is protocol-legal JSON the strict float decoder reads as
    a non-finite f64, so it reaches the wire while a Python `inf`
    never could."""
    client.stream.write(payload + "\n")
    client.stream.flush()
    return json.loads(client.stream.readline())


def plant_ctl(tool, addr, *argv):
    """One shipped `dcs-plant-ctl` invocation against the pair's
    spawned plant — the read and census probes the released tool
    decodes itself, and its own boundary's refusals of the non-finite
    spellings. Returns `(exit, stdout, stderr)` unasserted."""
    run = subprocess.run(
        [tool, addr, *[str(arg) for arg in argv]],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return run.returncode, run.stdout, run.stderr


def ctl_payload(invocation):
    """The decoded answer a successful `dcs-plant-ctl` invocation
    printed — None on a nonzero exit or an unprintable frame: a
    `{"float":null}` poisoning shape is exactly what the shipped
    binary cannot emit."""
    code, out, _err = invocation
    if code != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def ctl_refusal_name(invocation):
    """The refusal class a `dcs-plant-ctl` invocation answered:
    `parse` for the tool's own argument boundary, `wire` for a
    refusal the server carried back — or None when the invocation
    applied."""
    code, _out, err = invocation
    if code == 0:
        return None
    if CTL_VALUE_REFUSAL in err or CTL_STEP_REFUSAL in err:
        return "parse"
    if "cannot reach" in err:
        return "other"
    if "refused" in err or "invalid" in err:
        return "wire"
    return "other"


def finite_float(value):
    """A served Value dict's float payload when it is a finite
    number — the only Float the wire contract can spell — else
    None."""
    if isinstance(value, dict) and "float" in value:
        raw = value["float"]
        if (
            isinstance(raw, (int, float))
            and not isinstance(raw, bool)
            and math.isfinite(raw)
        ):
            return raw
    return None


def nonfinite_samples(entries):
    """The served-sample scan for the poisoning shape — each point
    entry whose float payload is not a finite number, the
    `{"float":null}` family the wire cannot spell."""
    bad = []
    for entry in entries:
        value = (entry.get("sample") or {}).get("value")
        if (
            isinstance(value, dict)
            and "float" in value
            and finite_float(value) is None
        ):
            bad.append(entry)
    return bad


def refusal_name(response):
    """The named refusal a non-finite probe's answer carries:
    `invalid_request` when the payload dies at the protocol's strict
    decode or the dispatched `dt` bound, `io.invalid_value` when it
    parses and the point-level contract refuses the value — or None
    on any other answer."""
    error = (response or {}).get("error")
    if not isinstance(error, dict):
        return None
    if error.get("kind") == "invalid_request":
        return "invalid_request"
    inner = error.get("error")
    if (
        error.get("kind") == "io"
        and isinstance(inner, dict)
        and "invalid_value" in inner
    ):
        return "io.invalid_value"
    return None


def claim_verdict(response):
    """A mutation answer's claim-state refusal — `fenced` or
    `unclaimed`, the io-carried fenced write verdict included — or
    None: the answers that mean the probe met the claim contract
    rather than the value contract under test."""
    error = (response or {}).get("error")
    if not isinstance(error, dict):
        return None
    if error.get("kind") in ("fenced", "unclaimed"):
        return error["kind"]
    inner = error.get("error")
    if (
        error.get("kind") == "io"
        and isinstance(inner, dict)
        and "fenced" in inner
    ):
        return "fenced"
    return None


def named_sources(model):
    """The emitted model's signal names mapped to their field
    sources — the declared junctions the point resolution prefers,
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
    is rewritten on every plant step, so the follow-up write's strict
    landed-equality applies only where no element outputs."""
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


def probe_point(model, points, driven):
    """The float field in-point the leg drives, `(point, undriven)` —
    the forcing junction `inflow` first, then the `net-flow` sum a
    poisoned write would feed the level integrators through, then any
    in-point the census serves holding a finite float — `undriven`
    reporting whether the dynamics leaves the point unwritten, the
    only shape the strict landed-equality checks apply to — or
    `(None, False)` when none does."""
    named = named_sources(model)
    field = {
        entry.get("point"): entry
        for entry in points
        if isinstance(entry, dict) and entry.get("direction") == "in"
    }
    for name in PREFERRED_POINTS:
        point = named.get(name, (None, None))[1]
        entry = field.get(point)
        if entry is not None and finite_float(
            (entry.get("sample") or {}).get("value")
        ) is not None:
            return point, point not in driven
    for point in sorted(field):
        if finite_float(
            (field[point].get("sample") or {}).get("value")
        ) is not None:
            return point, point not in driven
    return None, False


def nonfinite_refusal_pass(args, tamper):
    """The nonfinite-refusal run: converge, the shared-claim attach,
    the raw and tool-driven refusal probes, the unchanged census, the
    legitimate follow-up, and the pair's finite surface. Returns
    `(digest_entries, evidence, failures)`; raises `Inconclusive`
    where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "nonfinite-refusal leg has nothing to exercise"
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
        # owner's standing claim so a protocol-legal payload reaches
        # the value contract rather than the fencing verdict a foreign
        # attachment would meet. A release predating the seam records
        # no owner token the attach can name, or answers the verb's
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
        # never a hard-coded id — its stored sample, the plant tick
        # off a dt:0 step (the claim-holding probe that proves the
        # step path live while advancing nothing), and the post-step
        # census the refused payloads must leave untouched.
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
                "finite float to write"
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

        # Phase 4 — the probes under test: protocol-legal non-finite
        # payloads sent as raw lines — the spellings strict JSON
        # carries and the shipped tool cannot emit. Each must answer
        # the named refusal — never apply.
        probes = [
            (
                "the +inf write",
                f'{{"op":"write","point":{point},"value":{{"float":1e999}}}}',
                "done",
            ),
            (
                "the -inf write",
                f'{{"op":"write","point":{point},"value":{{"float":-1e999}}}}',
                "done",
            ),
            ("the +inf dt step", '{"op":"step","dt":1e999}', "stepped"),
            ("the negative dt step", '{"op":"step","dt":-1}', "stepped"),
        ]
        answers = {}
        for label, payload, applied in probes:
            answer = raw_request(probe_io, payload)
            if tamper == "expect-applied":
                # The doctored expectation: the leg asserts the
                # payloads applied — the plant's honest named refusal
                # must fail it, naming the actual answer.
                if answer.get("result") != applied:
                    failures.append(
                        f"{label} answered {answer} — the doctored expectation wanted the payload applied"
                    )
                    raise Abort
            elif answer.get("result") == applied:
                failures.append(
                    f"{label} was applied — the plant answered "
                    f"{answer} instead of the named refusal"
                )
                raise Abort
            else:
                name = refusal_name(answer)
                if name is None:
                    verdict = claim_verdict(answer)
                    if verdict is not None:
                        raise Inconclusive(
                            f"{label} met the {verdict} claim verdict "
                            "— the shared claim never covered the "
                            f"attachment: {answer}"
                        )
                    failures.append(
                        f"{label} answered off-contract — neither "
                        f"applied nor the named refusal: {answer}"
                    )
                    raise Abort
                answers[label] = name
        digest_entries.append(
            {"phase": "probes", "point": point, "answers": answers}
        )

        # Phase 5 — the shipped tool's own boundary: the released
        # `dcs-plant-ctl` driven at the deployed plant refuses the
        # non-finite spellings it cannot put on the wire and the
        # out-of-range `dt` the field's claim meets first — each a
        # named refusal, never an applied mutation.
        tool_probes = [
            ("write", plant_ctl(
                args.plant_ctl, rig.plant_addr, "write", point, "1e999"
            )),
            ("step-inf", plant_ctl(
                args.plant_ctl, rig.plant_addr, "step", "1e999"
            )),
            ("step-negative", plant_ctl(
                args.plant_ctl, rig.plant_addr, "step", "-1"
            )),
        ]
        tool_answers = {}
        for label, invocation in tool_probes:
            name = ctl_refusal_name(invocation)
            if name not in ("parse", "wire"):
                failures.append(
                    f"a dcs-plant-ctl {label} carrying the probe "
                    "payload was not refused with the named failure — "
                    f"exit {invocation[0]}: "
                    f"{invocation[2].strip() or invocation[1].strip()}"
                )
                raise Abort
            tool_answers[label] = name
        digest_entries.append(
            {"phase": "tool-refusals", "answers": tool_answers}
        )

        # Phase 6 — finite and unchanged: the refused payloads stored
        # nothing. The shipped client's read of the driven point and
        # census of the field are the decode half of the proof — an
        # answer the tool cannot print is the defect itself — and the
        # whole census must equal the pre-probe record: no step landed
        # between them, so element state holds exactly.
        read_back = plant_ctl(
            args.plant_ctl, rig.plant_addr, "read", point
        )
        served = ctl_payload(read_back)
        if not isinstance(served, dict) or served.get("result") != "sample":
            failures.append(
                "the refused payloads left state the shipped client "
                f"cannot deserialize — read on point {point} answered "
                f"exit {read_back[0]}: "
                f"{read_back[2].strip() or read_back[1].strip()}"
            )
            raise Abort
        served_value = (served.get("sample") or {}).get("value")
        if finite_float(served_value) is None:
            failures.append(
                "the driven point serves a non-finite float — the "
                "poisoning the refusal exists to prevent: "
                f"{served_value}"
            )
            raise Abort
        if served_value != baseline:
            failures.append(
                "the refused write left the stored value moved — "
                f"{baseline} -> {served_value}"
            )
            raise Abort
        census = plant_ctl(args.plant_ctl, rig.plant_addr, "list")
        listed = ctl_payload(census)
        if not isinstance(listed, dict) or listed.get("result") != "points":
            failures.append(
                "the post-refusal census no longer deserializes for "
                f"the shipped client — list answered exit "
                f"{census[0]}: "
                f"{census[2].strip() or census[1].strip()}"
            )
            raise Abort
        points1 = listed.get("points") or []
        poisoned = nonfinite_samples(points1)
        if poisoned:
            failures.append(
                f"point {poisoned[0].get('point')} serves a non-finite "
                "sample the wire cannot spell — the refused payloads "
                f"poisoned the field: {poisoned[0].get('sample')}"
            )
            raise Abort
        if points1 != points0:
            failures.append(
                "the field census moved between the refused payloads "
                "— element state did not hold: "
                f"{json.dumps(points1)[:400]}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "unchanged",
                "value": served_value,
                "points": len(points1),
            }
        )

        # Phase 7 — the follow-up: a finite write and step under the
        # same claim must behave normally — the proof no poisoned
        # accumulator or wedged claim survived the refused payloads —
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
                "the named refusals — the probes wedged the field: "
                f"{fin_write}"
            )
            raise Abort
        fin_step = probe_io.request({"op": "step", "dt": args.dt})
        if fin_step.get("result") != "stepped":
            failures.append(
                "a finite step under the same claim was refused after "
                f"the named refusals: {fin_step}"
            )
            raise Abort
        if (
            not isinstance(fin_step.get("tick"), int)
            or fin_step["tick"] <= tick0["tick"]
        ):
            failures.append(
                "the finite step answered stepped but the plant tick "
                f"never advanced — {tick0.get('tick')} -> "
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
        points2 = census2.get("points") if isinstance(census2, dict) else []
        poisoned = nonfinite_samples(points2)
        if poisoned:
            failures.append(
                "the follow-up write+step surfaced a non-finite "
                f"accumulator — point {poisoned[0].get('point')} "
                f"serves {poisoned[0].get('sample')}"
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
                f"shipped client — read answered exit "
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
                "the refused payloads moved the field owner's role — "
                f"GET /role answers {duty_role}"
            )
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the refused payloads moved the tracking peer's role "
                f"— GET /role answers {standby_role}"
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
                    "the refused payloads counted against the "
                    f"owner's io_health — {health0} -> {health1}"
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
        digest_entries, evidence, failures = nonfinite_refusal_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "nonfinite-refusal: the doctored expectation wanted the payload applied — an inconclusive run offers the doctored case no evidence"
            )
            return 1
        eprint(f"nonfinite-refusal: inconclusive — {inconclusive}")
        print(
            f"nonfinite-refusal-digest inconclusive — {inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"nonfinite-refusal: {line}")
        return 1
    for failure in failures:
        eprint(f"nonfinite-refusal: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"nonfinite-refusal: the {args.tamper} case passed "
                "silently — the leg never noticed the plant's "
                "honest refusal"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"nonfinite-refusal-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the non-finite writes and steps "
        f"on point {evidence['point']} refused by name with the "
        "census unchanged, the finite step advancing the plant to "
        f"tick {evidence['stepped_to']}, the pair's surface finite "
        f"through tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
