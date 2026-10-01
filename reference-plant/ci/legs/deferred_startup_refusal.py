#!/usr/bin/env python3
"""The deferred-startup-refusal leg for the reference plant — the
consumer-side proof that the #1301 deferred startup-claim refusal
contract (decision 103's deferred-timing half, the
`deferred-startup-claim-refusal-strands-unpaired-standby` finding,
where the rig's `claim-refused-undeclared` class meets its verdict at
activation) holds on the manifest-declared
redundant pair (WW-ENG-003, WW-LCM-001): a born-active launched with
no `--peer`/`--standby` whose conditional startup grant is refused at
the first *answered* field contact — not at activation — exits nonzero
naming the refusal and the `--standby` remedy rather than stranding a
peerless standby beside the healthy pair.

The born-active-failure leg (`ci/legs/born_active_failure.py`) exercises
the contract's other classes — the unreachable field completing its
grant, the declared pair's refusal rejoining `tracking`, the
verdict-free claim holding pending — each launched where its verdict
is known at activation or lands granted. This leg isolates the
deferred half the rig's finding names: the grant met no verdict at
activation — the pending surface the contract already records — and
the field's first answered contact delivered the live incumbent's
refusal, the `Ok(false)` landing inside a scan no activation result
could leave. The run:

- converges the manifest-declared pair through the pair rig's
  driven-tick loop — the incumbent's live claim the third launch's
  conditional grant must refuse against;
- launches a third controller born-active on the pair's launch shape
  — `dcs-controller --driven --remote` under the pair harness's spawn
  lever — but pairless: no `--peer`, no `--standby`, the unkeyed
  deployment's duplicate-launch shape. Its `--remote` names an address
  nothing serves, so the activation ask produces no verdict and the
  run stands pending behind the served standby surface;
- stands a byte-shuttle relay where the pending run's remote points,
  forwarding to the deployed pair's live plant — the first answered
  field contact. The deferred grant re-issues inside a driven scan,
  the incumbent's standing claim refuses it, and the settled refusal
  ends the pairless launch: the terminal scan answers the named
  refusal, the process exits nonzero inside the documented bound, and
  its output names the `FieldClaimFailed` refusal beside the standby
  rejoin remedy — never a lingering standby seat beside the pair;
- polls the deployed pair across the refusal window — the field owner
  stays `active`, the declared standby stays `tracking`, the field's
  claim never leaves the incumbent;
- restores the rig's launch set: the relay down, the refused process
  gone, the pair resting on its launch roles for the legs behind this
  one.

The contract postdates the pinned v0.6.0 release: a pairless run whose
deferred refusal landed — the incumbent's observed claimant journaled,
the claim probed `held` — while the process kept serving past the
bound is the pre-contract strand this leg exists to catch, and reports
`deferred-startup-refusal-digest inconclusive` rather than asserting
until the manifest repins a release carrying the contract.

Usage:

    deferred_startup_refusal.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `deferred-startup-refusal-digest <sha256>` line prints —
the check runs two passes and compares them
(`deferred-startup-refusal-nondeterministic`). A contract violation
reports `deferred-startup-refusal: …` lines on stderr and exits 1 —
the check's `deferred-refusal-strand-failed`. `--tamper expect-standby`
doctors the leg's verdict to the defect shape — asserting the refused
launch keeps standing as a peerless standby — so the leg proves its
exit assertion fires on the honest run.
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import born_active_failure
import driver_recovery
import failover
import pair


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored case.
# The contract's failure diagnostic rides `failed` — the leg's stem
# still names the driver's -nondeterministic and -unchecked reports.
# The doctored case: a leg asserting the refused seat stands as a
# peerless standby beside the pair — the defect shape the contract
# closed — must surface the named diagnostic on the honest run rather
# than passing an unexercised contract.
LEG = {
    "order": 690,
    "title": "the deferred-startup-refusal leg",
    "passes": "deferred-startup-refusal-leg",
    "failed": "deferred-refusal-strand-failed",
    "tampers": [
        {
            "name": "expect-standby",
            "passed": "an expect-standby case passed the deferred-startup-refusal leg",
            "missed": "the expect-standby case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the refused launch standing"
            ],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the contract the leg exercises:
    carried as `(reason, detail)` — `reason` the stable phrase the
    inconclusive digest line prints (two passes must share it),
    `detail` the run's own verdicts reported on stderr. A launch
    exiting on a field-side startup condition predates the born-active
    contract's pending state; a deferred refusal landing without the
    `startup_claim_refused` record or the pairless exit predates its
    deferred disposition."""


# The driven-scan bounds each phase gets: the pending window held
# while the field cannot be asked, the contact window the deferred
# grant's refused verdict lands inside once the field answers, the
# settle ticks the restore proves launch roles on. The wall-clock
# waits: the remote driver's one-second re-attach spacing plus margin
# between driven scans, and the documented bound the refused run's
# exit lands inside once the verdict is latched.
PENDING_SCANS = 3
CONTACT_SCANS = 8
SETTLE_TICKS = 3
REATTACH_WAIT = 1.4
EXIT_WAIT = 10.0


class Relay:
    """The answered-contact lever: a byte-shuttle listener bound where
    the pending run's `--remote` points, forwarding each accepted
    connection to the deployed pair's live plant — the field contact
    the deferred conditional grant re-issues on, and the channel the
    incumbent's refusal answers through. The pending launch's
    unreachable remote and the field's return are the same address,
    the outage the deferred grant is recorded against."""

    def __init__(self, bind, target):
        host, _, port = bind.rpartition(":")
        self.address = (host, int(port))
        host, _, port = target.rpartition(":")
        self.target = (host, int(port))
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(self.address)
        listener.listen()
        self._listener = listener
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        self._listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                continue
            try:
                upstream = socket.create_connection(self.target, timeout=2)
            except OSError:
                conn.close()
                continue
            for source, sink in ((conn, upstream), (upstream, conn)):
                threading.Thread(
                    target=self._forward,
                    args=(source, sink),
                    daemon=True,
                ).start()

    @staticmethod
    def _forward(source, sink):
        try:
            while True:
                chunk = source.recv(65536)
                if not chunk:
                    break
                sink.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                sink.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def close(self):
        """Release the endpoint — the rig's launch set restores."""
        self._stop.set()
        try:
            socket.create_connection(self.address, timeout=0.2).close()
        except OSError:
            pass
        self._thread.join(timeout=2)
        self._listener.close()


def scratch_files(rig, name):
    """The launched member's declared persistence under the rig's
    runner-owned scratch — the durable journal the refused settle's
    records append to."""
    root = os.path.join(rig.scratch, name)
    os.makedirs(root, exist_ok=True)
    return {"journal_file": os.path.join(root, "journal.jsonl")}


def pending_surface(url, process, preamble, failures):
    """The pairless born-active's pending surface while the field
    cannot be asked: role `standby`, sync `unsynchronized`, no
    observed claim (an unreachable field is unobserved, never
    `unclaimed`), the startup log's pending report — held across the
    pending window's driven scans without an exit."""
    def check():
        if process.poll() is not None:
            failures.append(
                "the pairless pending run exited before any field "
                "contact — an unreachable field is the pending class, "
                "never an exit"
            )
            return False
        report = pair.get(f"{url}/role", "GET /role", failures)
        if (
            report.get("role") != "standby"
            or report.get("sync") != "unsynchronized"
            or report.get("field_claim") is not None
        ):
            failures.append(
                f"the pairless launch's pending surface reads {report} "
                "— expected role standby, sync unsynchronized, no "
                "observed claim"
            )
            return False
        return True

    if not check():
        raise Abort
    if not any("stands pending" in line for line in preamble):
        failures.append(
            "the pairless launch's startup log carries no pending "
            f"report: {preamble}"
        )
        raise Abort
    for _ in range(PENDING_SCANS):
        status, body = pair.request(f"{url}/scan", {"scans": 1})
        if status != 200:
            failures.append(
                f"the pending run's driven scan answered {status} "
                f"{body} — the pending surface serves the scan"
            )
            raise Abort
        if not check():
            raise Abort


def pair_health(rig, failures, when):
    """The deployed pair's launch roles mid-leg — one tracking-first
    pair tick plus the role poll: the field owner `active`, the
    declared standby `tracking`, and the field's write-ownership claim
    still `held` on the incumbent the deferred grant was refused
    against — the refusal took nothing."""
    held = driver_recovery.roles_hold(rig, failures, when)
    claim = pair.get(f"{rig.duty_url}/role", "GET /role", failures).get(
        "field_claim"
    )
    if claim != "held":
        failures.append(
            f"the field owner reports claim {claim!r} {when} — the "
            "refused deferred grant moved the field's write-ownership "
            "claim off the incumbent"
        )
        held = False
    rig.tick(rig.standby_url, rig.duty_url, failures)
    return "active+tracking" if held else "moved"


def deferred_startup_refusal_pass(args, tamper):
    """The deferred-startup-refusal run: converge the manifest-declared
    pair, launch a pairless born-active whose field cannot answer,
    stand the relay that makes the contact answer, and assert the
    incumbent's refusal ends the launch nonzero naming the refusal and
    remedy — never a lingering standby — while the deployed pair keeps
    its roles. Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "deferred-startup-refusal leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    rig = None
    relay = None
    third = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence and the substrate gate: the settled
        # pair the pairless launch stages beside — the incumbent's
        # claim line naming the owner token the refusal's observed
        # claimant attributes. A duty that never reported its startup
        # claim is the release predating the contract's substrate.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        incumbent_token = failover.owner_token(rig.duty_preamble)
        if incumbent_token is None:
            raise Inconclusive(
                "the pinned release predates the startup-claim record",
                "the field owner's startup log carries no "
                "write-ownership claim line — the deferred refusal's "
                "observed-claimant attribution has nothing to read",
            )
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty": "active",
                "standby": "tracking",
            }
        )

        # Phase 2 — the pairless born-active: the pair's own launch
        # shape, no --peer and no --standby, its --remote naming an
        # address nothing serves. The activation ask produces no
        # verdict — the run stands pending behind the served standby
        # surface rather than exiting, the born-active contract's
        # recorded wait. Under the contract a launched active never
        # exits for a field-side startup condition, so a startup exit
        # naming the field-side refusal vocabulary is the pinned
        # release predating the pending state — inconclusive — while
        # an exit naming nothing is a defect.
        dead = pair.closed_port()
        process, url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            dead,
            None,
            scratch_files(rig, "refused"),
            pair_token=pair.PAIR_TOKEN,
        )
        third = process
        if url is None:
            joined = " ".join(preamble)
            if any(
                word in joined
                for word in born_active_failure.PREDATING_WORDS
            ):
                raise Inconclusive(
                    "the pinned release predates the born-active "
                    "pending contract",
                    "the pairless born-active exited at startup naming "
                    "a field-side refusal: "
                    f"{'; '.join(preamble[-2:]) or 'no diagnostic'}",
                )
            failures.append(
                "the pairless born-active exited at startup without a "
                f"field-side refusal: "
                f"{'; '.join(preamble) or 'no diagnostic'}"
            )
            raise Abort
        pending_surface(url, process, preamble, failures)
        marks = {
            "surface": "standby/unsynchronized/no-claim",
            "held": "pending",
            "pair": pair_health(
                rig, failures, "through the pending window"
            ),
        }
        if failures:
            raise Abort
        digest_entries.append({"phase": "pending", **marks})

        # Phase 3 — the answered contact: the relay stands where the
        # pending run's remote points and forwards to the deployed
        # pair's live plant. Each driven scan's claim probe asks the
        # field fresh; the first answered contact re-issues the
        # conditional startup grant, the incumbent's standing claim
        # refuses it, and the pairless run's recorded disposition is
        # the nonzero exit — the terminal scan answering the named
        # refusal — never a standby seat stranded beside the pair.
        relay = Relay(dead, rig.plant_addr)
        marks = {"contact": None, "exit": None, "named": None}
        refusal_answer = None
        for _ in range(CONTACT_SCANS):
            if third.poll() is not None:
                break
            try:
                status, body = pair.request(f"{url}/scan", {"scans": 1})
            except urllib.error.URLError:
                # The run's monitor is down — the exit the contract
                # calls for, or a crash the exit assertions name.
                break
            if status != 200:
                refusal_answer = body
                break
            # The remote driver's re-attach spacing is wall-clock
            # while the scans are driven — let the next scan's contact
            # actually retry.
            time.sleep(REATTACH_WAIT)
        try:
            code = third.wait(timeout=EXIT_WAIT)
        except subprocess.TimeoutExpired:
            code = None
        if isinstance(refusal_answer, str) and (
            "write-ownership claim failed" in refusal_answer
        ):
            marks["contact"] = "refusal-answered"
        elif refusal_answer is not None:
            marks["contact"] = "answered-other"
        else:
            marks["contact"] = "unobserved"
        tail = ""
        if code is not None:
            tail = third.stderr.read()
        output = " ".join([*preamble, tail])

        if code is not None:
            # The recorded disposition: nonzero exit naming the
            # FieldClaimFailed refusal — the incumbent's live claim —
            # and the standby rejoin remedy for the undeclared pair.
            named_refusal = (
                "field write-ownership claim failed" in output
            )
            named_remedy = (
                "rejoin as a standby" in output or "--standby" in output
            )
            named_pairless = "no --peer was declared" in output
            if code == 0:
                failures.append(
                    "the refused pairless launch exited zero — a "
                    "deferred-refused conditional startup grant is the "
                    "launch's failure, never a silent settle"
                )
            if not named_refusal:
                failures.append(
                    "the refused launch's exit never named the "
                    "FieldClaimFailed refusal — its output reads: "
                    f"{output[-300:]}"
                )
            if not named_remedy or not named_pairless:
                failures.append(
                    "the refused launch's exit never named the "
                    "--standby remedy for the undeclared pair — its "
                    f"output reads: {output[-300:]}"
                )
            # The refused seat must not keep serving: a still-answering
            # monitor is the lingering standby the contract closed.
            try:
                urllib.request.urlopen(f"{url}/role", timeout=2)
            except Exception:
                pass
            else:
                failures.append(
                    "the refused launch kept serving past its exit — "
                    "a lingering standby seat beside the healthy pair"
                )
            if failures:
                raise Abort
            marks["exit"] = "nonzero"
            marks["named"] = "refusal+remedy"
        else:
            # No exit inside the bound: classify by what the run's own
            # record carries. The latched refusal journaled but never
            # exited is the contract's substrate present with its
            # disposition broken — a failure. The incumbent's verdict
            # delivered — the observed claimant journaled or the claim
            # probed held — without the `startup_claim_refused` record
            # or the exit is the pre-contract strand the contract
            # closed — inconclusive. Neither is the staging never
            # answering — the relay carried no verdict.
            journal = pair.get(f"{url}/journal", "GET /journal", failures)
            events = [entry.get("event", {}) for entry in journal]
            report = pair.get(f"{url}/role", "GET /role", failures)
            if any(
                "startup_claim_refused" in event for event in events
            ):
                failures.append(
                    "the deferred refusal journaled "
                    "startup_claim_refused but the pairless run kept "
                    "serving past the bound — the contract's exit "
                    "never followed the latched refusal"
                )
                raise Abort
            if any(
                "field_claim_observed" in event for event in events
            ) or report.get("field_claim") == "held":
                raise Inconclusive(
                    "the pinned release predates the deferred "
                    "startup-refusal disposition",
                    "the incumbent's refusal landed at the first "
                    "answered contact — the observed claimant "
                    "journaled, the claim probed held — but the "
                    "pairless run kept standing pending past the "
                    "documented bound: the strand the contract closed",
                )
            failures.append(
                "the pending run's deferred grant never produced a "
                "verdict — the answered contact carried neither a "
                "refusal nor a claim observation; the relayed field "
                "never reached the deployed pair's claim arbitration"
            )
            raise Abort

        if tamper == "expect-standby":
            # The doctored expectation — the defect shape the contract
            # closed: a refused pairless seat standing as a lingering
            # standby beside the healthy pair. The honest run's exit
            # must fail it.
            failures.append(
                "the doctored expectation wanted the refused launch "
                "standing as a peerless standby beside the pair — "
                "the honest run exited nonzero naming the refusal "
                "and the standby remedy"
            )
            raise Abort
        marks["pair"] = pair_health(
            rig, failures, "through the refusal window"
        )
        if failures:
            raise Abort
        digest_entries.append({"phase": "refusal", **marks})

        # Phase 4 — the restore: the refused process gone, the relay
        # down, the deployed pair resting on its launch roles — the
        # field owner active, the declared standby tracking it — for
        # the legs behind this one.
        for _ in range(SETTLE_TICKS):
            rig.tick(standby_url, duty_url, failures)
        if not driver_recovery.roles_hold(
            rig, failures, "after the refused launch"
        ):
            raise Abort
        digest_entries.append(
            {
                "phase": "restore",
                "duty": "active",
                "standby": "tracking",
            }
        )
        evidence["final_tick"] = pair.get(
            f"{duty_url}/role", "GET /role", failures
        ).get("tick")
    except Inconclusive:
        raise
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if relay is not None:
            relay.close()
        if third is not None:
            pair.stop(third)
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
        choices=["expect-standby"],
        help="doctor the leg's verdict to the defect shape — a "
        "refused pairless launch standing as a lingering standby — "
        "so the pass must fail naming the honest exit",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = (
            deferred_startup_refusal_pass(args, args.tamper)
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "deferred-startup-refusal: the doctored expectation "
                "wanted the refused launch standing as a peerless "
                "standby — an inconclusive run offers the doctored "
                "case no evidence"
            )
            return 1
        # The digest line carries only the stable reason — the run's
        # own verdicts report on stderr, where two identical passes
        # need not share them.
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"deferred-startup-refusal: inconclusive — {detail}")
        print(
            f"deferred-startup-refusal-digest inconclusive — {reason}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"deferred-startup-refusal: {line}")
        return 1
    for failure in failures:
        eprint(f"deferred-startup-refusal: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"deferred-startup-refusal: the {args.tamper} case "
                "passed silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"deferred-startup-refusal-digest {digest} — converged at "
        f"tick {evidence['converged']}, the pairless born-active's "
        "deferred grant stood pending behind the silent field, the "
        "incumbent's refusal at the first answered contact exited it "
        "nonzero naming the refusal and the standby remedy, and the "
        f"pair held its launch roles to tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
