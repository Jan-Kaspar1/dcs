#!/usr/bin/env python3
"""The self-standby-refusal leg for the reference plant — the
consumer-side proof that #1340's self-address tracking-source refusal
holds at the declaration the customer actually ships, not just at raw
argv (WW-ENG-003, WW-LCM-001 — the
`self-tracking-standby-never-refused` finding), mirrored at the
customer boundary alongside the qa rig's self-standby-refusal
scenario.

A tracking source naming the instance's own `--listen` socket passed
every check the release line knew: each pull returns the run's own
checkpoint, which a standby's document always stamps
`source_owns_field: false`, so every apply scores a heartbeat miss. An
unarmed run then reports `standby` covering no peer forever, and the
armed one the reference deployment declares — `controllers[].failover_budget`
riding `--auto-promote` — manufactures a failover against itself at
every budget. A self-referential standby claim is therefore not a
legitimate standby seat at all: it is a usage error, refused at parse
before the run exists, because a deployment that wires a standby from
its own service label must learn so at launch rather than discover a
peerless seat indistinguishable from a legitimate one.

The reference deployment's labeled manifest peers are exactly where a
customer project writes that wiring: `deploy/manifest.json` names each
member's service label and its `standby` label names the member it
follows, so the self-addressed declaration is one field edited —
`ctrl-b` following `ctrl-b` instead of `ctrl-a` — and the deployed
pair stage is where it lands. The run:

- converges the manifest-declared pair on its own unmodified wiring —
  the field owner `active`, the declared standby `tracking` it — the
  legitimate seat the self-addressed launch must never occupy;
- doctors the pair stage twice, on scratch copies of the deployment
  manifest so the checked-in one stays pristine. The declared
  standby's `standby` label follows its own member's instantiated
  endpoint — the label a customer writes from its own service name,
  a wildcard bind dialed through loopback the same substitution the
  serving peer applies to a wildcard `?peer=` announce — and the
  doctor's second case names a member no deployment resolves, the
  unresolvable-target control;
- deploys each declaration on the declared standby's own launch shape
  — `dcs-controller --driven --remote` at the pair's live plant, with
  its declared persistence under the leg's scratch and the declared
  `failover_budget` arming its self-promotion — and asserts the
  self-addressed one is refused at startup, before it reports a
  listener, naming the tracking target, the listener it resolves onto,
  and the rule the tracking source must be a different instance;
- asserts the refused seat never occupied the pair's: it served no
  monitor at all, neither peer's served journal gained a role walk
  from it, and the deployed pair kept its launch roles with the
  field's write-ownership claim held on the launch owner across the
  refusal window — the line keeps scanning while the seat asks;
- proves the clean pair still settles, tracks, and promotes: the
  documented switch — `POST /demote` on the field owner,
  `POST /promote` on the converged standby — moving the field, then
  the same switch run once more to re-seat the launch roles for the
  legs behind this one.

The contract postdates older release lines, and the pinned release may
precede it: a self-addressed launch that reaches its own monitor — the
pre-#1340 strand, where every pull answers with the run's own
non-owning checkpoint — or one whose launch answers an unrecognized
`--standby` flag, is reported `self-standby-refusal-digest
inconclusive` rather than asserted. A launch refused for some other
reason, one refused without naming the rule, and a tracking source
naming no declared member that a release refuses at startup, are all
failures: the contract demands the refusal name what it refused, and
leave the unresolvable source on the pull-miss path.

Usage:

    self_standby_refusal.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `self-standby-refusal-digest <sha256>` line prints —
the check runs two passes and compares them
(`self-standby-refusal-nondeterministic`). A contract violation
reports `self-standby-refusal: …` lines on stderr and exits 1 — the
check's `self-standby-refusal-failed`. `--tamper expect-serving-standby`
doctors the leg's own expectation — the self-addressed launch refused
by name AND standing as the pair's legitimate standby, a disposition
no release can honestly produce — so the leg proves its
refusal-versus-seat classification fires rather than passing an
unexercised contract.
"""

import argparse
import json
import os
import shutil
import socket
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import driver_recovery
import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored case.
# The doctored case: a leg asserting the self-addressed launch is
# refused by name AND stands as the pair's legitimate standby — an
# impossible disposition — must surface the named diagnostic rather
# than passing an unexercised contract.
LEG = {
    # The next free slot after the legs origin/main added past the
    # claim-class ones (claim-skew-bound 730, pending-source-pull
    # 740, demote-release-stays-released 750) — the stage runs the
    # legs in this order and no two may share one.
    "order": 760,
    "title": "the self-standby-refusal leg",
    "passes": "self-standby-refusal-leg",
    "tampers": [
        {
            "name": "expect-serving-standby",
            "passed": "an expect-serving-standby case passed the self-standby-refusal leg",
            "missed": "the expect-serving-standby case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the self-addressed launch refused and standing as the pair's standby"
            ],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the self-address tracking-source
    refusal this leg exercises — or the doctored declaration names no
    member the deployment instantiates. Carried as `(reason, detail)`:
    `reason` the stable phrase the `inconclusive` digest line prints
    (two passes must share it), `detail` the run's own verdicts,
    reported on stderr only, where runner-assigned ports belong. A
    self-addressed launch that reached its own listener is the
    pre-#1340 strand the contract closed — reported here, never as a
    product failure of a release that simply predates it."""


# The refusal vocabulary the self-addressed launch's own record must
# carry: the subject — the tracking target resolving onto this run's
# own listener — and the rule that refuses it. A refusal naming the
# flag but not these names no self-address, so an operator reading the
# exit cannot tell which wiring was rejected.
SUBJECT_MARK = "resolves to this instance's own --listen socket"
RULE_MARK = "the tracking source must be a different instance"

# The pair ticks each refusal window is observed across (the line keeps
# scanning while the self-addressed seat asks, so the verdict is read
# on a live line), and the settle ticks the pair rests on its launch
# roles before the documented switch.
WINDOW_TICKS = 2
SETTLE_TICKS = 3

# The doctored expectation's stable evidence prefix — every failure
# the tampered pass records carries it, so the check's negative case
# finds it whether the honest run refused the self-address or a
# predating release offered the case no evidence at all.
TAMPER_EVIDENCE = (
    "the doctored expectation wanted the self-addressed launch refused "
    "and standing as the pair's standby"
)


def free_port(avoid=()):
    """A port nothing listens on — the runner-assigned port a declared
    listen endpoint is instantiated on. The self-addressed tracking
    target must name that port *before* the run exists, so unlike
    `pair.listen_bind`'s ephemeral bind the leg picks it itself.
    `avoid` holds ports a case must not land on — the deployment's own
    declared port, so a label naming no member cannot resolve onto the
    case's listener through a wildcard resolver."""
    while True:
        stream = socket.socket()
        stream.bind(("127.0.0.1", 0))
        port = stream.getsockname()[1]
        stream.close()
        if port not in avoid:
            return port


def instantiated(entry, port):
    """The declared member's `listen` endpoint with the leg's
    runner-assigned port in place of the deployment's — the manifest's
    bind host preserved (the reference deployment's wildcard) the same
    way `pair.listen_bind` maps it, with a port the leg knows ahead of
    the launch."""
    listen = entry.get("listen") or "127.0.0.1:0"
    return f"{listen.rsplit(':', 1)[0]}:{port}"


def doctored_manifest(args, scratch, case, label):
    """A scratch copy of the deployment manifest whose declared
    standby follows `label` instead of the field owner it tracks — the
    self-addressed tracking declaration a customer project ships when
    its standby's wiring is written from its own service name. The
    checked-in manifest stays pristine. Returns `(path, declared
    port)` — the port the original label carried, kept so the
    unresolvable case's label cannot collide with its own listener."""
    with open(args.manifest) as handle:
        document = json.load(handle)
    standbys = [
        entry for entry in document["controllers"] if "standby" in entry
    ]
    if len(standbys) != 1:
        raise Abort(
            "the manifest declares no single standby pair — the "
            "self-standby-refusal leg has nothing to doctor"
        )
    entry = standbys[0]
    declared_port = entry["standby"].rpartition(":")[2]
    entry["standby"] = f"{label}:{declared_port}"
    path = os.path.join(scratch, f"manifest-{case}.json")
    with open(path, "w") as handle:
        json.dump(document, handle, indent=2)
    return path, declared_port


def standby_entry(manifest_path, name):
    """The doctored manifest's declared standby entry — the doctor's
    own launch shape, read back out of the declaration the leg
    deployed rather than out of the checked-in manifest."""
    with open(manifest_path) as handle:
        document = json.load(handle)
    for entry in document["controllers"]:
        if entry["name"] == name:
            return entry
    raise Abort(f"the doctored manifest declares no member {name}")


def tracking_target(entry, endpoints):
    """The endpoint a declared `standby` label resolves to: the named
    member's instantiated listen address, a wildcard bind dialed
    through loopback — the same substitution the serving peer applies
    to a wildcard `?peer=` announce — or the label's own spelling where
    it names no instantiated member, the unresolvable tracking source
    the contract keeps on the pull-miss path."""
    label = entry["standby"]
    endpoint = endpoints.get(label.rpartition(":")[0])
    return label if endpoint is None else pair.dialable(endpoint)


def served_role(url):
    """The role report a self-addressed run that reached its own
    listener serves — the pre-contract surface the refusal closes: a
    run whose every pull answers with its own non-owning checkpoint.
    Best-effort; the run is stopped either way."""
    try:
        return simulate.http(f"{url}/role")
    except Exception:
        return None


def refusal_line(preamble):
    """The startup line naming the self-address, or None where no line
    carries one."""
    for line in preamble:
        if SUBJECT_MARK in line:
            return line
    return None


def pair_health(rig, failures, when):
    """The deployed pair's launch roles mid-leg — one tracking-first
    pair tick plus the role poll: the field owner `active`, the
    declared standby `tracking`, and the field's write-ownership claim
    still held on the launch owner — the refused seat took nothing.
    Returns "active+tracking" or "moved"."""
    held = driver_recovery.roles_hold(rig, failures, when)
    claim = pair.get(f"{rig.duty_url}/role", "GET /role", failures).get(
        "field_claim"
    )
    if claim != "held":
        failures.append(
            f"the field owner reports claim {claim!r} {when} — the "
            "refused seat moved the field's write-ownership claim off "
            "the launch owner"
        )
        held = False
    rig.tick(rig.standby_url, rig.duty_url, failures)
    return "active+tracking" if held else "moved"


def seat_untouched(rig, floors, failures, when):
    """The deployed pair across a refused seat — the launch-roles poll
    with the field's claim still held, and both peers' served journals
    read above the floors the seat opened: a launch that occupied the
    pair's legitimate standby seat would journal its own role walk
    there. Returns False when the seat moved the pair."""
    held = pair_health(rig, failures, when) == "active+tracking"
    for url, name, floor in floors:
        journal = pair.get(f"{url}/journal", "GET /journal", failures)
        walked = [
            entry
            for entry in journal[floor:]
            if "role_changed" in entry.get("event", {})
        ]
        if walked:
            failures.append(
                f"{name}'s served journal gained the role walks {walked} "
                f"{when} — the self-addressed seat occupied a pair seat"
            )
            held = False
    return held


def self_standby_refusal_pass(args, tamper):
    """The self-standby-refusal run: converge the declared pair,
    deploy the self-addressed tracking declaration on the declared
    standby's own launch shape and assert it is refused at startup
    before it serves, prove the unresolvable tracking source is still
    the degraded-source contract, and settle, track, and promote the
    clean pair afterwards. Returns `(digest_entries, evidence,
    failures)`; raises `Inconclusive` where the pinned release
    predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "self-standby-refusal leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    scratch = tempfile.mkdtemp(prefix="dcs-self-standby-")
    rig = None
    seat = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — the legitimate pair: the declared deployment
        # converged on its own wiring, the seat the self-addressed
        # launch below must never occupy.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "control",
                "ticks": converged["ticks"],
                "duty": "active",
                "standby": "tracking",
            }
        )

        # The journal floors the refused seat must leave untouched.
        floors = [
            (
                url,
                rig.peer_name(url),
                len(pair.get(f"{url}/journal", "GET /journal", failures)),
            )
            for url in (duty_url, standby_url)
        ]

        # Phase 2 — the doctored declarations. The first follows the
        # declared standby's own label; the second names a member no
        # deployment resolves, so the refusal the leg asserts is
        # provably targeted at the self-address rather than at every
        # declared `--standby` a release happens to refuse.
        results = []
        for case, label in (
            ("self-labelled", standby_decl["name"]),
            ("undeclared-labelled", "ctrl-z"),
        ):
            doctored, declared_port = doctored_manifest(
                args, scratch, case, label
            )
            entry = standby_entry(doctored, standby_decl["name"])
            port = free_port(avoid={declared_port})
            listen = instantiated(entry, port)
            target = tracking_target(entry, {entry["name"]: listen})
            process, url, preamble = pair.spawn_peer(
                args.controller,
                args.model,
                args.dt,
                rig.plant_addr,
                target,
                pair.persistence_files(scratch, entry),
                auto_promote=entry.get("failover_budget"),
                listen=listen,
                pair_token=pair.PAIR_TOKEN,
            )
            seat = process

            if case == "undeclared-labelled":
                # The control: a tracking source naming no declared
                # member is the pull-miss contract — a run that serves
                # and degrades, the documented degraded source — never a
                # startup error. A release refusing it would refuse
                # every declared standby and pass this leg vacuously.
                if url is None:
                    failures.append(
                        f"the {case} declaration {entry['standby']} "
                        "exited at startup — a tracking source naming "
                        "no declared member is the degraded-source "
                        "contract, never a startup refusal: "
                        f"{'; '.join(preamble) or 'no diagnostic'}"
                    )
                    raise Abort
                evidence[f"{case}-role"] = served_role(url)
                pair.stop(process)
                seat = None
                results.append(
                    {
                        "case": case,
                        "target": "undeclared-peer",
                        "launched": "served-degraded",
                        "seat": "undeclared-source",
                    }
                )
                continue

            if url is not None:
                # The self-addressed launch reached its own listener:
                # the pre-#1340 shape, where every pull answers with
                # the run's own non-owning checkpoint. Its role report
                # is recorded before the run is stopped.
                evidence[f"{case}-role"] = served_role(url)
                pair.stop(process)
                seat = None
                raise Inconclusive(
                    "the pinned release predates the self-address "
                    "tracking-source refusal",
                    f"the {case} declaration {entry['standby']} launched "
                    "and served the instance's own monitor instead of "
                    "being refused at startup: "
                    f"{json.dumps(evidence[f'{case}-role'])[:200]}",
                )

            code = process.returncode
            named = refusal_line(preamble)
            output = " ".join(preamble)
            if named is None:
                if "unknown option" in output or "unrecognized" in output:
                    raise Inconclusive(
                        "the pinned release predates the --standby "
                        "vocabulary the self-addressed declaration is "
                        "written on",
                        f"the {case} launch reported "
                        f"{'; '.join(preamble[-2:]) or 'no diagnostic'}",
                    )
                failures.append(
                    f"the {case} declaration {entry['standby']} exited "
                    f"{code} naming no self-address refusal — the leg "
                    "can only judge a refusal that names what it "
                    f"refused: {'; '.join(preamble) or 'no diagnostic'}"
                )
                raise Abort
            if code == 0:
                failures.append(
                    f"the {case} declaration exited zero — a "
                    "self-addressed tracking source is a usage error, "
                    "never a silent seat"
                )
                raise Abort
            for flag, value in (("--standby", target), ("--listen", listen)):
                if flag not in named:
                    failures.append(
                        f"the {case} refusal never names its {flag} "
                        f"argument: {named}"
                    )
                elif value not in named:
                    failures.append(
                        f"the {case} refusal names {flag} but not the "
                        f"endpoint {value} it resolved: {named}"
                    )
            if RULE_MARK not in named:
                failures.append(
                    "the self-addressed refusal never names the rule it "
                    f"enforces — an operator reading the exit cannot "
                    f"tell what to declare instead: {named}"
                )
            if tamper is not None:
                # The doctored expectation — the defect shape the
                # contract closed: the self-addressed seat refused by
                # name and standing as the pair's legitimate standby.
                # The honest refusal must fail it.
                failures.append(
                    f"{TAMPER_EVIDENCE} — the {case} launch exited "
                    f"{code} before reporting a listener, naming the "
                    "self-address and the rule"
                )
                raise Abort
            if failures:
                raise Abort
            evidence[f"{case}-refusal"] = named
            eprint(f"self-standby-refusal: {case} refused — {named}")
            results.append(
                {
                    "case": case,
                    "target": "own-listener",
                    "launched": "refused-at-parse",
                    "seat": "never-served",
                }
            )
            pair.stop(process)
            seat = None

        # The refused seat never occupied the pair's legitimate
        # standby: it served no monitor, neither peer journaled a role
        # walk from it, and the pair kept its launch roles with the
        # field's claim held on the launch owner while it asked.
        for _ in range(WINDOW_TICKS):
            if not seat_untouched(
                rig, floors, failures, "across the refused seat"
            ):
                raise Abort
        digest_entries.append({"phase": "refusal", "cases": results})
        digest_entries.append(
            {
                "phase": "pair",
                "roles": "active+tracking",
                "claim": "launch-owner",
                "journal": "no-role-walk",
            }
        )

        # Phase 3 — the clean pair still settles, tracks, and
        # promotes: the declared pair rests on its launch roles, then
        # runs the documented switch — `POST /demote` on the field
        # owner, `POST /promote` on the converged standby.
        for _ in range(SETTLE_TICKS):
            rig.tick(standby_url, duty_url, failures)
        if not driver_recovery.roles_hold(
            rig, failures, "after the refused seat"
        ):
            raise Abort
        switched = rig.switch(duty_url, standby_url, failures)
        digest_entries.append(
            {
                "phase": "switch",
                "ticks": switched["ticks"],
                "demoted": switched["demoted_role"].get("role"),
                "promoted": switched["promoted_role"].get("role"),
            }
        )

        # The restore: the same switch run once more, seating the
        # launch roles for the legs behind this one.
        restored = rig.switch(
            standby_url,
            duty_url,
            failures,
            demote_what="the promoted standby",
            promote_what="the converged ex-owner",
        )
        if not driver_recovery.roles_hold(
            rig, failures, "after the restore switch"
        ):
            raise Abort
        digest_entries.append(
            {
                "phase": "restore",
                "ticks": restored["ticks"],
                "duty": "active",
                "standby": "tracking",
            }
        )
        evidence["final_tick"] = restored["ticks"][-1]
    except Inconclusive:
        raise
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if seat is not None:
            pair.stop(seat)
        if rig is not None:
            rig.close()
        shutil.rmtree(scratch, ignore_errors=True)
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
        choices=["expect-serving-standby"],
        help="doctor the leg's own expectation — the self-addressed "
        "launch must be refused by name AND stand as the pair's "
        "legitimate standby, a disposition no release can honestly "
        "produce — so the pass must fail naming the honest refusal",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = self_standby_refusal_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"self-standby-refusal: {TAMPER_EVIDENCE} — an "
                "inconclusive run offers the doctored case no evidence"
            )
            return 1
        # The digest line carries only the stable reason — the run's
        # own verdicts and runner-assigned endpoints report on stderr,
        # where two identical passes need not share them.
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"self-standby-refusal: inconclusive — {detail}")
        print(f"self-standby-refusal-digest inconclusive — {reason}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"self-standby-refusal: {line}")
        return 1
    for failure in failures:
        eprint(f"self-standby-refusal: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"self-standby-refusal: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"self-standby-refusal-digest {digest} — the declared pair "
        f"converged on its own wiring at tick {evidence['converged']}, "
        "the self-addressed standby declaration was refused at startup "
        "naming the tracking target, the listener it resolved onto, "
        "and the rule, while an unresolvable tracking source still "
        "served as the degraded source, the pair's legitimate standby "
        "seat kept tracking through the refusal window and promoted "
        f"on the documented switch, and the launch roles were restored "
        f"to tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())