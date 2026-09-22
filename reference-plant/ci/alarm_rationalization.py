#!/usr/bin/env python3
"""The pair contract's alarm-rationalization leg — decision 70's
declared-once record proven reaching the operator boundary on the
deployed consumer pair (WW-ENG-003, WW-ALM-001).

The alarm-validation stage (ci/alarm_validation.py) proves the
rejection half of the record on a customer-owned document — every
managed alarm instance carrying its `rationalization` block and
`priority`/`class`/`response_ticks` codes, doctored copies refused by
the released `dcs-controller --check` — and audits a lone driven
controller's served sections; the pair leg proves the declared pair
changes over bumplessly. Neither proves the record reaches the
operator boundary on the *deployed pair*: the one declaration the
controller, the signal index, and the snapshot all serve — on the
field owner and on the tracking standby whose adopted state must
serve it identically, and still after a changeover hands the field
to the other peer. This leg does. The run:

- loads the emitted model's managed alarm instances — each
  `managed-latching-alarm`/`managed-bool-latching-alarm` instance's
  `rationalization` block (consequence, required action, and the
  display/procedure reference) plus its declared
  `priority`/`class`/`response_ticks` parameters — the declared-once
  record the alarm-validation stage audits on the document;
- converges the manifest-declared pair through the pair leg's
  driven-tick loop;
- audits both peers' served surfaces: `GET /signals`' components
  section must carry each instance's declared `rationalization`
  block verbatim and `GET /snapshot`'s parameters section must serve
  each alarm's declared `priority`/`class`/`response_ticks` live —
  every mismatch or dropped record failing by the instance's
  `<kind>:<id>` name;
- issues the documented demote/promote switch and re-audits both
  peers' served sections — the same declared record on the promoted
  owner and on the reconverged tracker, the single declaration
  riding the checkpoint rather than re-derived per peer;
- restores the pair's launch roles — the manifest-declared duty
  controller `active` and its standby `tracking` again.

Usage:

    alarm_rationalization.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `alarm-rationalization-digest <sha256>` line prints —
the check runs two passes and compares them
(`alarm-rationalization-nondeterministic`). A contract violation
reports `alarm-rationalization: …` lines on stderr and exits 1 — the
check's `alarm-rationalization-failed`. The `--tamper` cases doctor
the served sections the audit reads: `dropped-record` drops one
managed alarm's served component record and parameter report, and
`rewritten-field` rewrites one's served `required_action` prose and
`priority` value — each must fail naming the instance rather than
pass a served record that dropped or rewrote a declared field.
"""

import argparse
import hashlib
import json
import sys

import alarm_validation
import pair


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


def declared_record(model):
    """The emitted model's declared-once alarm record — one entry per
    managed alarm instance carrying its `rationalization` block and
    its `priority`/`class`/`response_ticks` codes, keyed by the
    `<kind>:<id>` name the served surface reports."""
    return {
        alarm_validation.instance_name(instance): {
            "rationalization": instance.get("rationalization"),
            "codes": {
                key: (instance.get("parameters") or {}).get(key)
                for key in alarm_validation.CODE_PARAMETERS
            },
        }
        for instance in alarm_validation.managed_alarms(model)
    }


def doctor_served(model, signals, snapshot, tamper):
    """Copies of a peer's served sections with one managed alarm's
    record broken — the served-side defect shapes the audit must
    name: `dropped-record` drops the first managed alarm's component
    record and parameter report outright; `rewritten-field` rewrites
    its served `required_action` prose and `priority` value."""
    signals = json.loads(json.dumps(signals))
    snapshot = json.loads(json.dumps(snapshot))
    target = alarm_validation.instance_name(
        alarm_validation.managed_alarms(model)[0]
    )
    if tamper == "dropped-record":
        signals["components"] = [
            entry
            for entry in signals.get("components") or []
            if entry.get("name") != target
        ]
        snapshot["parameters"] = [
            entry
            for entry in snapshot.get("parameters") or []
            if entry.get("name") != target
        ]
    elif tamper == "rewritten-field":
        for entry in signals.get("components") or []:
            if entry.get("name") == target:
                block = entry.setdefault("rationalization", {})
                block["required_action"] = "a rewritten procedure"
        for entry in snapshot.get("parameters") or []:
            if entry.get("name") == target:
                entry.setdefault("values", {})["priority"] = {"int": 99}
    else:
        raise Abort(f"unknown tamper {tamper}")
    return signals, snapshot


def audit_peer(model, peer, url, failures, tamper=None):
    """One peer's served alarm record against the emitted model's
    declaration: `GET /signals`' components section carries every
    managed alarm instance's `rationalization` block verbatim and
    `GET /snapshot`'s parameters section serves its declared codes
    live — failures naming the peer and the offending instance.
    Returns the per-instance record the peer served, for the
    digest."""
    signals = pair.get(
        f"{url}/signals", f"GET /signals on {peer}", failures
    )
    snapshot = pair.get(
        f"{url}/snapshot", f"GET /snapshot on {peer}", failures
    )
    if tamper is not None:
        signals, snapshot = doctor_served(model, signals, snapshot, tamper)
    for miss in alarm_validation.record_misses(model, signals, snapshot):
        failures.append(f"{peer}: {miss}")
    components = {
        entry.get("name"): entry for entry in signals.get("components") or []
    }
    reported = {
        entry.get("name"): (entry.get("values") or {})
        for entry in snapshot.get("parameters") or []
    }
    record = {}
    for instance in alarm_validation.managed_alarms(model):
        name = alarm_validation.instance_name(instance)
        record[name] = {
            "rationalization": (components.get(name) or {}).get(
                "rationalization"
            ),
            "codes": {
                key: (reported.get(name) or {}).get(key)
                for key in alarm_validation.CODE_PARAMETERS
            },
        }
    return record


def rationalization_pass(args, tamper):
    """The alarm-rationalization run: converge, audit both peers'
    served records, switch, re-audit, restore. Returns
    `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "alarm-rationalization leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    declared_alarms = declared_record(model)
    if not declared_alarms:
        raise Abort(
            "the emitted model declares no managed alarm instance — "
            "the rationalization leg has nothing to exercise"
        )

    digest_entries, evidence, failures = [], {}, []
    digest_entries.append({"phase": "declared", "instances": declared_alarms})
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        peers = (
            (duty_decl["name"], rig.duty_url),
            (standby_decl["name"], rig.standby_url),
        )

        # Phase 1 — convergence: the pair leg's driven-tick loop, the
        # tracking peer scanned first so each pull applies the owner's
        # latest checkpoint and the peers rest identical.
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

        # Phase 2 — the settled pair's served record: both peers must
        # serve every managed alarm instance's declared
        # rationalization block verbatim and its declared codes live —
        # the tracking standby's adopted state serving the same single
        # declaration, not a second copy.
        served = {
            peer: audit_peer(model, peer, url, failures, tamper)
            for peer, url in peers
        }
        if failures:
            raise Abort
        evidence["instances"] = len(declared_alarms)
        digest_entries.append({"phase": "served", "peers": served})

        # Phase 3 — the documented switch: demote the field owner,
        # promote the converged standby, the handover ticks proving
        # the run continues bumplessly.
        switched = rig.switch(rig.duty_url, rig.standby_url, failures)
        evidence["switched_at"] = switched["demote"]["tick"]
        digest_entries.append(
            {
                "phase": "switch",
                "demote": switched["demote"],
                "promote": switched["promote"],
                "ticks": switched["ticks"],
            }
        )

        # Phase 4 — the record unchanged across the changeover: the
        # promoted owner and the reconverged tracker serve the same
        # declared record — the declaration riding the checkpoint,
        # never re-derived per peer.
        served = {
            peer: audit_peer(model, peer, url, failures, tamper)
            for peer, url in peers
        }
        if failures:
            raise Abort
        digest_entries.append(
            {"phase": "served-after-switch", "peers": served}
        )

        # Phase 5 — the roles restore: demote the new owner, promote
        # the reconverged peer, and drive the pair back to the
        # manifest's declared arrangement — the duty controller
        # `active`, its standby `tracking`.
        demote = rig.demote(
            rig.standby_url, failures, "the new field owner"
        )
        promote = rig.promote(
            rig.duty_url, failures, "the reconverged peer"
        )
        ticks = []
        for _ in range(pair.HANDOVER_TICKS):
            _tracked, owner = pair.tick(
                rig.standby_url, rig.duty_url, failures
            )
            ticks.append(owner["tick"])
        duty_role = pair.get(
            f"{rig.duty_url}/role", "GET /role", failures
        )
        standby_role = pair.get(
            f"{rig.standby_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active":
            failures.append(
                f"the restored field owner reports "
                f"{duty_role.get('role')!r}, expected active — the "
                "pair was not left in its declared roles"
            )
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the restored standby never reconverged — GET /role "
                f"answers {standby_role}"
            )
        if failures:
            raise Abort
        evidence["restored_at"] = ticks[-1]
        digest_entries.append(
            {
                "phase": "restore",
                "demote": demote,
                "promote": promote,
                "ticks": ticks,
                "duty_role": duty_role,
                "standby_role": standby_role,
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
        choices=["dropped-record", "rewritten-field"],
        help="doctor the served sections the audit reads — the pass "
        "must fail naming the instance whose record dropped or "
        "rewrote a declared field",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = rationalization_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"alarm-rationalization: {line}")
        return 1
    for failure in failures:
        eprint(f"alarm-rationalization: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"alarm-rationalization: the {args.tamper} case "
                "passed silently — the leg never noticed the doctored "
                "served record"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"alarm-rationalization-digest {digest} — "
        f"{evidence['instances']} managed alarm instances served "
        f"verbatim on both peers, tracking by tick "
        f"{evidence['converged']}, switched at tick "
        f"{evidence['switched_at']}, the record unchanged across the "
        f"switch, roles restored at tick {evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
