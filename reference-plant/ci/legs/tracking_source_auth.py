#!/usr/bin/env python3
"""The tracking-source-auth leg for the reference plant — the
consumer-side proof that the documented `GET /checkpoint?peer=` announce
seam cannot be forged into a tracking source on the customer's declared
deployment: a fabricated hint must not unblock the demote guard, must
not silently redirect an established tracking source, and must not
inject a forged checkpoint without a journal entry (WW-ENG-003,
WW-LCM-001 — the announced-hint authenticity contract #684 settled,
mirrored at the customer boundary).

The peer-announce leg (`ci/legs/peer_announce.py`) proves the seam's
acceptance half — a serving monitor records an announce only when it
names the pulling connection's own source address. The
announced-source-verify leg (`ci/legs/announced_source_verify.py`) proves
that one announced forgery never unblocks the guard. This leg proves the
whole authenticity surface on the manifest-declared pair: every
`?peer=` variant the settled contract must refuse, driven in one pass
through the shared `launch_pair` rig, with the hostile endpoint the
sibling legs share (`announced_source_verify.ForeignEndpoint`) recording
every pull that reaches it. The run:

- the guard with no source — the declared standby brought up wired at an
  address nothing serves, so no genuine announce ever lands and the
  declared duty controller is the deployment's unsourced instance. Three
  announce variants stand between it and the demote guard: none at all,
  a crafted announce naming a dead address on a *foreign* IP (refused at
  the serving monitor — the read still answers the owner's own document,
  a refused announce is silent, never an error), and a live foreign
  endpoint's announce naming this host's own loopback source so it lands
  and serves a forged checkpoint. Each must leave `POST /demote`
  answering the named `409 no_tracking_source`: no fabricated hint
  unblocks the guard, and the landed one is refused with the durable
  `tracking_source_refused` record naming the endpoint — never adopted;
- the redirect against an established source — with the pair converged
  `active`/`tracking` on the declared wiring, a `?peer=` announce
  naming the foreign endpoint is aimed at the *tracking standby's*
  monitor, whose pull target is already established on its configured
  `--standby` source. The hint must not redirect it: the standby keeps
  reporting `tracking` on its real successor, its served checkpoint
  keeps naming its line owner, and its journal records no adoption
  naming the foreign endpoint;
- the legitimate announces — the pair's own tracking pulls keep
  announcing the genuine monitor addresses, and the pair stays exactly
  one `active` plus one `tracking` standby through the redirect attempt;
- the restore — the pair's launch roles stand for the next leg.

The contract postdates some pinned release lines: where the launched
tooling predates it — a served checkpoint without the
`source_owns_field` stamp, the declared `journal_file` persistence
absent, or no durable `tracking_source_refused` record at all — the leg
reports `tracking-source-auth-digest inconclusive` rather than asserting
until the manifest repins a release carrying the contract.

Usage:

    tracking_source_auth.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `tracking-source-auth-digest <sha256>` line prints — the
check runs two passes and compares them
(`tracking-source-auth-nondeterministic`). A contract violation reports
`tracking-source-auth: …` lines on stderr and exits 1 — the check's
`tracking-source-auth-failed`. `--tamper expect-unblocked` doctors the
leg's expectation to the pre-contract shape — asserting a fabricated
`?peer=` hint unblocks the demote guard on an unsourced instance, the
defect #684 reported — so the leg proves its refusal fires on the honest
`no_tracking_source` rather than passing an unexercised contract.
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import announced_source_verify
import claim_reclaim
import pair
import simulate
import tracking_source_fallback

ForeignEndpoint = announced_source_verify.ForeignEndpoint
forged_document = announced_source_verify.forged_document
refusals = announced_source_verify.refusals
adopted_ports = announced_source_verify.adopted_ports
adopted_sources = announced_source_verify.adopted_sources


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting a fabricated `?peer=` hint
# unblocks the demote guard on an unsourced instance must surface the
# named diagnostic on the honest `no_tracking_source` rather than
# passing an unexercised contract.
LEG = {
    "order": 122,
    "title": "the tracking-source-auth leg",
    "passes": "tracking-source-auth",
    "tampers": [
        {
            "name": "expect-unblocked",
            "passed": "an expect-unblocked case passed the tracking-source-auth leg",
            "missed": "the expect-unblocked case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the fabricated "
                "announce to unblock the demote guard"
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
# endpoint across the whole announcement-variant sweep: one bounded pull
# per hint per verify window, never a pull target followed.
VERIFY_PULL_BOUND = 4

# The driven-scan bound the pair's reconvergence and the launch-role
# restore each run inside.
CONVERGE_SCANS = 12

# The address the crafted foreign announce names — an address the
# pulling connection never is, so the serving monitor's own-source
# acceptance check refuses it. A refused announce is silently ignored:
# the checkpoint read still answers the serving peer's own document.
FOREIGN_HOST = "10.255.255.1"


def foreign_announce():
    """The `?peer=` address a crafted announce names — a just-released
    ephemeral port moved onto `FOREIGN_HOST`, off the pulling
    connection's own loopback source, so the serving monitor refuses it
    and records nothing."""
    closed = pair.closed_port()
    return f"{FOREIGN_HOST}:{closed.rsplit(':', 1)[1]}"


def served_checkpoint(url, failures):
    """The field owner's own served checkpoint read — a refused
    announce leaves this read unchanged, so the read is how a refused
    announce is told from a landed one."""
    answer = pair.get(f"{url}/checkpoint", "GET /checkpoint", failures)
    if not isinstance(answer, dict) or "tick" not in answer:
        failures.append(
            f"GET /checkpoint answered {answer!r} — the serving peer "
            "served no document"
        )
        raise Abort
    return answer


def guard_answer(url, failures):
    """`POST /demote`'s `(status, body)` on the unsourced duty
    controller — the demote guard's own answer, read without letting a
    refusal raise."""
    return pair.request(f"{url}/demote", {})


def tracking_source_auth_pass(args, tamper):
    """The exercised run: sweep the announce variants against the
    demote guard on the deployment's unsourced instance, then aim a
    redirect hint at the tracking standby's established source and
    prove the pair's legitimate announces keep it tracking. Returns
    `(digest_entries, evidence, failures)`; raises `Inconclusive` where
    the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "tracking-source-auth leg has nothing to exercise"
        )
    if announced_source_verify.rig_journal_absent(declared):
        raise Inconclusive(
            "the manifest's declared pair carries no journal_file — "
            "the durable refusal audit is absent"
        )
    _manifest, duty_decl, standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    rig = None
    foreign = None
    try:
        # The unsourced instance: the declared standby comes up wired at
        # an address nothing serves, so no genuine announce ever lands
        # and the declared duty controller — launched without any
        # `--standby` — is the deployment's unsourced instance, the one
        # the demote guard protects.
        rig = pair.launch_pair(args, declared, tamper="broken-peer-flag")
        duty_url = rig.duty_url
        urls = {
            duty_decl["name"]: duty_url,
            standby_decl["name"]: rig.standby_url,
        }
        journal_file = rig.duty_files["journal_file"]
        if rig.duty_files.get("journal_file") is None:
            raise Inconclusive(
                "the manifest's duty controller declares no "
                "journal_file — the durable refusal audit is absent"
            )
        own = served_checkpoint(duty_url, failures)
        forged = forged_document(own)
        if forged is None:
            raise Inconclusive(
                "the unsourced instance serves no field-owning "
                "checkpoint document — the pinned release predates the "
                f"announced-hint authenticity contract the leg reads: {own}"
            )

        foreign = ForeignEndpoint(forged)

        # Variant 1 — no announce at all: the guard stands on its own.
        floor = len(pair.journal_records(journal_file))
        bare = guard_answer(duty_url, failures)
        # Variant 2 — a crafted announce naming a dead address on a
        # foreign IP: the serving monitor refuses it, and the read is
        # unchanged.
        crafted = foreign_announce()
        served = pair.get(
            f"{duty_url}/checkpoint?peer={crafted}",
            f"GET /checkpoint?peer={crafted}",
            failures,
        )
        if not isinstance(served, dict) or served.get("tick") != own.get("tick"):
            failures.append(
                f"the crafted foreign announce's read answered {served!r} "
                f"where the unsourced instance's own checkpoint reads "
                f"tick {own.get('tick')!r} — a refused announce is "
                "silently ignored, never an error"
            )
            raise Abort
        crafted_answer = guard_answer(duty_url, failures)
        # Variant 3 — the foreign endpoint's live announce: it lands —
        # naming this host's own loopback source — and serves a forged
        # checkpoint. A landed hint is a verify candidate, never a pull
        # target, so the guard still refuses.
        landed = foreign.announce(duty_url, failures)
        landed_answer = guard_answer(duty_url, failures)
        pulls = foreign.served_pulls()
        refusals_here = [
            (source, detail)
            for source, detail in refusals(journal_file, floor)
            if str(source).endswith(":" + str(foreign.port))
        ]
        adoptions = adopted_ports(journal_file)
        role = pair.get(f"{duty_url}/role", "GET /role", failures)

        variants = {
            "bare": bare,
            "crafted": crafted_answer,
            "landed": landed_answer,
        }
        evidence["variants"] = {
            name: {"status": status, "body": body}
            for name, (status, body) in variants.items()
        }
        evidence["landed_at"] = landed["tick"]
        evidence["verify_pulls"] = len(pulls)
        evidence["refused"] = [detail for _s, detail in refusals_here]
        evidence["adopted_ports"] = adoptions

        if tamper == "expect-unblocked":
            # The doctored expectation: a fabricated `?peer=` hint
            # unblocks the guard on an unsourced instance. Whichever way
            # the demotes answered, the pass fails naming it.
            failures.append(
                "the doctored expectation wanted the fabricated "
                "announce to unblock the demote guard — the sweep "
                "answered "
                + ", ".join(
                    f"{name} {status} {body}"
                    for name, (status, body) in sorted(variants.items())
                )
            )
            raise Abort
        for name, (status, body) in sorted(variants.items()):
            if status != 409 or body != "no_tracking_source":
                failures.append(
                    f"the {name} announce variant left POST /demote "
                    f"answering {status} {body} — the guard's named "
                    "refusal is 409 no_tracking_source"
                )
        if len(pulls) > VERIFY_PULL_BOUND:
            failures.append(
                f"the demote guard spent {len(pulls)} pulls at one "
                "unproven endpoint across the variant sweep — a "
                "recorded hint is a verify candidate, never a pull "
                "target"
            )
        if str(foreign.port) in adoptions:
            failures.append(
                "the unsourced instance's journaled adoptions name the "
                f"foreign endpoint's port {foreign.port} — the forged "
                "announce was adopted"
            )
        if role.get("role") != "active":
            failures.append(
                "the unsourced instance stopped owning the field across "
                f"the announce sweep — GET /role answers {role}"
            )
        if not refusals_here:
            raise Inconclusive(
                "the forged endpoint's refused probe journaled no "
                "tracking_source_refused record — the pinned release "
                "predates the durable announced-hint audit the leg reads"
            )
        digest_entries.append(
            {
                "phase": "guard",
                "variants": {
                    name: {"status": status, "body": body}
                    for name, (status, body) in sorted(variants.items())
                },
                "verify_pulls": len(pulls),
                "adopted_foreign": str(foreign.port) in adoptions,
                "role": role.get("role"),
                "refused": sorted(detail for _s, detail in refusals_here),
            }
        )

        # The redirect attempt — the declared standby relaunched on the
        # owner's real monitor address so the pair converges and the
        # tracking peer's pull target is established on its configured
        # `--standby` source.
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

        pre_adoptions = adopted_ports(rig.standby_files["journal_file"])
        redirect = foreign.address
        answered = pair.get(
            f"{standby_url}/checkpoint?peer={redirect}",
            f"GET /checkpoint?peer={redirect}",
            failures,
        )
        if not isinstance(answered, dict) or "tick" not in answered:
            failures.append(
                f"the redirect announce's read answered {answered!r} — "
                "the tracking standby served no document"
            )
            raise Abort
        after = rig.converge(failures, count=CONVERGE_SCANS)
        redirect_role = after["standby_role"]
        redirect_doc = pair.get(
            f"{standby_url}/checkpoint", "GET /checkpoint", failures
        )
        post_adoptions = adopted_ports(rig.standby_files["journal_file"])
        duties = [
            name
            for name, report in (
                (duty_decl["name"], after["duty_role"]),
                (standby_decl["name"], redirect_role),
            )
            if report.get("role") == "active"
        ]
        evidence["redirect"] = {
            "announce": redirect,
            "answered_at": answered["tick"],
            "tracking_role": redirect_role,
            "line_owner": redirect_doc.get("line_owner"),
            "adopted_ports": post_adoptions,
            "actives": duties,
        }
        if not claim_reclaim.tracking(redirect_role):
            failures.append(
                "the redirect announce knocked the tracking standby off "
                f"its configured source — GET /role answers {redirect_role}"
            )
        if redirect_doc.get("source_owns_field") is not False:
            failures.append(
                "the tracking standby's served checkpoint claims "
                f"source_owns_field {redirect_doc.get('source_owns_field')!r} "
                "— a tracking peer never claims the field"
            )
        if str(foreign.port) in post_adoptions:
            failures.append(
                "the tracking standby's journaled adoptions name the "
                f"foreign endpoint's port {foreign.port} — the redirect "
                "hint silently retargeted its pulls"
            )
        if post_adoptions != pre_adoptions:
            failures.append(
                "the tracking standby journaled new adoptions "
                f"{post_adoptions} where the redirect attempt left "
                f"{pre_adoptions} — an established source is never "
                "retargeted by a bare announce"
            )
        line_owner = redirect_doc.get("line_owner")
        if not str(line_owner).endswith(
            ":" + tracking_source_fallback.monitor_port(duty_url)
        ):
            failures.append(
                "the tracking standby's served checkpoint names "
                f"line_owner {line_owner!r} — the redirect hint moved the "
                "established source off its real field owner"
            )
        if duties != [duty_decl["name"]]:
            failures.append(
                f"the pair reports {duties} active through the redirect "
                f"attempt — the legitimate announces keep exactly "
                f"[{duty_decl['name']!r}] alone"
            )
        digest_entries.append(
            {
                "phase": "redirect",
                "answered_at": answered["tick"],
                "tracking_role": redirect_role,
                # The propagated owner stamp carries the field owner's
                # ephemeral monitor port, so it reaches the digest as
                # the pair member it resolves to.
                "line_owner": tracking_source_fallback.peer_kind(
                    urls, line_owner
                ),
                "adopted": [
                    tracking_source_fallback.peer_kind(urls, source)
                    for source in adopted_sources(
                        rig.standby_files["journal_file"]
                    )
                ],
                "actives": [
                    duty_decl["name"] if name == duty_decl["name"] else "other"
                    for name in duties
                ],
            }
        )
        evidence["restored"] = after["ticks"][-1]
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
        choices=["expect-unblocked"],
        help="doctor the leg's expectation to the pre-contract shape — "
        "asserting a fabricated ?peer= hint unblocks the demote guard "
        "on an unsourced instance — so the pass must fail naming the "
        "refusal it observed",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = tracking_source_auth_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "tracking-source-auth: an inconclusive run under "
                f"the {args.tamper} doctor offers the doctored case no "
                "evidence"
            )
            return 1
        eprint(f"tracking-source-auth: inconclusive — {inconclusive}")
        print(f"tracking-source-auth-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"tracking-source-auth: {line}")
        return 1
    for failure in failures:
        eprint(f"tracking-source-auth: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"tracking-source-auth: the {args.tamper} case passed "
                "silently — the leg never noticed the fabricated "
                "announce unblocking the guard"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"tracking-source-auth-digest {digest} — the bare, crafted and "
        f"landed announce variants all left the unsourced instance's "
        f"demote guard answering no_tracking_source after "
        f"{evidence['verify_pulls']} bounded verify pull, the pair "
        f"reconverged by tick {evidence['converged']} and kept tracking "
        f"through the redirect attempt at tick {evidence['restored']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
