#!/usr/bin/env python3
"""The peer-announce leg for the reference plant — the consumer-side
proof that the checkpoint `?peer=` announce acceptance contract holds
on the deployed redundant pair (WW-ENG-003, WW-LCM-001).

The pair leg (`ci/pair.py`) proves the demoted peer follows the
address a tracking peer announced on its pulls — the follow-peer half
of the tracking-source contract. This leg proves the announce's
*acceptance* half on the same customer-owned pair: the serving
monitor records a `GET /checkpoint?peer=<addr>` announce only when it
names the pulling connection's own source address, so a foreign
client cannot rewrite the tracking source a demoted field owner
follows — the poisoning that would strand it `unsynchronized` and
unpromotable. The rig is the pair legs' shared one —
`pair.launch_pair` reads the standby wiring and persistence fields
out of `deploy/manifest.json` and spawns the released tooling exactly
as the manifest declares. The run:

- convergence — the pair leg's driven-tick loop converges the standby
  to `tracking`: each driven tick's tracking pull announces the
  standby's own monitor address on the field owner, the genuine
  announce the demotion's tracking-source fallback records;
- the crafted announce — a foreign `GET /checkpoint?peer=<addr>`
  against the field owner's monitor naming a dead address on a
  foreign IP — the `closed_port` convention moved off the pulling
  connection's own source. The checkpoint read still answers the
  owner's checkpoint — a refused announce is ignored, never an
  error;
- the switch — the documented `demote`/`promote`, with the same
  crafted announce issued at the decisive point: after the promote's
  final-sync pull has re-announced the genuine standby address on the
  demoted peer's monitor, but before the demoted peer's first
  tracking pull. Refused there, the demoted peer tracks its real
  successor into `tracking` — and the source the demotion verified
  and adopted is pinned, so even a landed rewrite at that point
  could not redirect the pulls; a landed announce *before* the
  demote is the case the hardened contract must answer — it joins
  the bounded announced set beside the genuine ones instead of
  evicting them, and the demotion's per-candidate verification skips
  the endpoint no checkpoint pull can verify and adopts the verified
  standby: the journaled `tracking_source_adopted` is the audit. (A
  landed announce that ever outlived every genuine one still meets
  the same verify-at-demote rule: with no verifiable candidate the
  demotion refuses `no_tracking_source` rather than stranding the
  peer on a dead pull.);
- the restore — the same switch back leaves the manifest-declared
  duty controller `active` and its standby `tracking` again.

Usage:

    peer_announce.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `peer-announce-digest <sha256>` line prints — the check
runs two passes and compares them (`peer-announce-nondeterministic`).
A contract violation reports `peer-announce: …` lines on stderr and
exits 1 — the check's `peer-announce-failed`. `--tamper
landed-announce` doctors the crafted announce onto the pulling
connection's own source address — a closed local port — so it lands
exactly as it would on a controller whose acceptance check regressed,
and doctors the adoption assertion to require that planted address:
a healthy demotion verifies past the dead hint, journals
`tracking_source_adopted` naming the genuine standby, and the leg
reports the mismatch — while a blind last-announcer adoption would
satisfy the doctored expectation and pass silently.
"""

import argparse
import hashlib
import json
import sys

import pair


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The foreign IP the crafted announce names — an address the pulling
# connection never is, so the serving monitor's own-source check
# refuses it. The `landed-announce` tamper instead names the
# connection's own loopback source — accepted, as a regressed
# contract would accept the foreign one.
FOREIGN_HOST = "10.255.255.1"


def crafted_peer(tamper):
    """The `?peer=` address the crafted announce names — a closed port
    either way, so a landed announce records a hint no checkpoint
    pull can verify: the demotion's per-candidate verification skips
    it for the verified standby, and the doctored assertion expects
    it to be the journaled `tracking_source_adopted` instead. The
    honest leg moves the just-released port onto `FOREIGN_HOST`,
    naming a source the pulling connection does not own; the
    `landed-announce` tamper keeps the loopback source so the
    announce lands."""
    closed = pair.closed_port()
    if tamper == "landed-announce":
        return closed
    return f"{FOREIGN_HOST}:{closed.rsplit(':', 1)[1]}"


def adopted_source(journal_path):
    """The most recent `tracking_source_adopted` event's full
    `host:port` the journal file records — the demotion's audit of
    which announced endpoint it verified and pinned. Unlike
    `pair.journal_records` the port is kept: the landed-announce
    tamper's whole proof is which port the adoption named."""
    adopted = None
    with open(journal_path) as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry = record.get("entry")
            if not isinstance(entry, dict):
                continue
            event = entry.get("event", {}).get("tracking_source_adopted")
            if isinstance(event, dict) and event.get("source"):
                adopted = event["source"]
    return adopted


def crafted_announce(url, peer, tick, fingerprint, failures):
    """Issue the crafted `GET /checkpoint?peer=<peer>` against the
    monitor at `url`, asserting the read still answers the serving
    peer's own checkpoint — a refused announce is ignored, so the
    endpoint answers `200` either way and only what the demotion later
    follows distinguishes a landed announce from a refused one.
    Returns the answered checkpoint's identifying fields."""
    checkpoint = pair.get(
        f"{url}/checkpoint?peer={peer}",
        "GET /checkpoint?peer=<crafted>",
        failures,
    )
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("tick") != tick
        or checkpoint.get("model_fingerprint") != fingerprint
    ):
        failures.append(
            f"the checkpoint read answered {checkpoint}, expected "
            f"the serving peer's checkpoint at tick {tick} under "
            f"fingerprint {fingerprint}"
        )
        raise Abort
    return {
        "tick": checkpoint["tick"],
        "model_fingerprint": checkpoint["model_fingerprint"],
    }


def announce_pass(args, tamper):
    """The announce run: converge, craft, switch, restore. Returns
    `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the peer-announce "
            "leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence: the pair leg's driven-tick loop, the
        # tracking peer scanned first so each pull applies the owner's
        # latest checkpoint and the peers rest identical. Every pull
        # announces the standby's own monitor address on the field
        # owner — the genuine announce the demotion's tracking-source
        # fallback records.
        converged = rig.converge(failures)
        owner = converged["owner"]
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # Phase 2 — the crafted announce against the field owner's
        # monitor while the pair stands settled: a foreign
        # `GET /checkpoint?peer=` naming a dead address that is not
        # the pulling connection's own. The read still answers the
        # owner's checkpoint — the refusal is silent, the announce
        # simply never lands.
        crafted = crafted_peer(tamper)
        served = crafted_announce(
            duty_url, crafted, owner["tick"], rig.fingerprint, failures
        )
        evidence["answered_at"] = served["tick"]
        digest_entries.append({"phase": "announce", "checkpoint": served})

        # Phase 3 — the documented switch: demote the field owner,
        # promote the converged standby. The promote's final-sync pull
        # re-announces the genuine standby address on the demoted
        # peer's monitor — so the same crafted announce is issued
        # again at the decisive point, after that last genuine write
        # and before the demoted peer's first tracking pull. The
        # demotion already verified the genuine hint and pinned the
        # adoption — refused or landed, the rewrite cannot redirect
        # the demoted peer's pulls, and the peer reconverges
        # `tracking` on its real successor. A crafted announce that
        # landed *before* the demote is the case the hardened
        # contract answers at the demote itself: the hint it planted
        # never verifies, so `POST /demote` refuses
        # `no_tracking_source` — the acceptance contract's observable
        # proof under the tamper.
        decisive = {}

        def attempt():
            decisive["checkpoint"] = crafted_announce(
                duty_url, crafted, owner["tick"], rig.fingerprint, failures
            )

        switched = rig.switch(
            duty_url,
            standby_url,
            failures,
            after_promote=attempt,
            audit_receipts=True,
        )
        evidence["reconverged"] = switched["ticks"][-1]
        digest_entries.append(
            {
                "phase": "switch",
                "demote": switched["demote"],
                "promote": switched["promote"],
                "decisive": decisive["checkpoint"],
                "ticks": switched["ticks"],
                "demoted_role": switched["demoted_role"],
                "promoted_role": switched["promoted_role"],
                "receipts": switched["receipts"],
            }
        )

        # The landed-announce tamper's assertion: the planted hint
        # joined the announced set beside the genuine one, so the
        # demotion's per-candidate verification — not the announce
        # itself — is what answers it. The journaled
        # `tracking_source_adopted` names which endpoint the demotion
        # verified and pinned; the tamper doctors that expectation to
        # require the planted address, so a healthy adoption — the
        # verified standby over the unverifiable hint — reports the
        # named mismatch while a blind last-announcer adoption would
        # satisfy the doctored expectation and pass silently.
        if tamper == "landed-announce":
            adopted = adopted_source(rig.duty_files["journal_file"])
            if adopted != crafted:
                failures.append(
                    "the demotion did not follow the landed announce — "
                    "its journal's tracking_source_adopted names "
                    f"{adopted or 'nothing'}, expected the planted "
                    f"{crafted}"
                )
                raise Abort

        # Phase 4 — the restore: the same switch back leaves the pair
        # in the manifest's declared arrangement — the duty controller
        # `active`, its standby `tracking`.
        restored = rig.switch(
            standby_url,
            duty_url,
            failures,
            demote_what="the promoted peer",
            promote_what="the reconverged peer",
            audit_receipts=True,
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
            }
        )
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
        choices=["landed-announce"],
        help="doctor the crafted announce onto the pulling "
        "connection's own source address so it lands — the "
        "demotion's journaled tracking_source_adopted must name "
        "that planted address, so a healthy verified adoption "
        "reports the mismatch",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = announce_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"peer-announce: {line}")
        return 1
    for failure in failures:
        eprint(f"peer-announce: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"peer-announce: the {args.tamper} case passed "
                "silently — the leg never noticed the crafted "
                "announce landing"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"peer-announce-digest {digest} — tracking by tick "
        f"{evidence['converged']}, checkpoint answered at tick "
        f"{evidence['answered_at']}, demoted peer tracking its "
        f"successor by tick {evidence['reconverged']}, launch roles "
        f"restored at tick {evidence['restored']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
