#!/usr/bin/env python3
"""The graceful-shutdown leg for the reference plant — the
consumer-boundary mirror of the rig leg (WW-ENG-003, WW-LCM-001),
pinning the graceful SIGTERM shutdown contract on the
customer-owned deployment through released tooling: a SIGTERM to a
manifest-declared controller stops the paced scan at a scan boundary
inside the documented bound, flushes the latest checkpoint through
the declared --state-file mount, releases the held plant write claim
on the way out — a successor never fenced by the dead claim — and
exits 0, with a second signal forcing prompt exit and a relaunched
controller resuming the persisted tick domain while the pair
reconverges to one active plus one tracking standby.

The run, on the manifest-declared driven pair through the shared
pair rig:

- converges the pair to `tracking` and settles one kind-declared
  command `applied` into both peers' adopted receipt log — the
  pre-stop run whose receipts the resume must carry;
- records the field owner's served tick, then delivers SIGTERM to the
  duty process (`send_signal(SIGTERM)` — the harness form of the
  runner's `docker kill --signal=SIGTERM`, with no SIGKILL
  follow-up) and requires it exited within the stop bound with exit
  0 and the graceful-shutdown marker on its stderr — only a failure
  exits nonzero, naming the reason;
- reads the duty's declared state file and requires the flushed
  checkpoint at the stopped tick under the manifest's fingerprint —
  a lost flush fails;
- relaunches the duty onto its declared files and requires the
  startup preamble resumed at the persisted tick, the served
  snapshot standing at it, the adopted receipt log one identical
  log, and the pair reconverged — the relaunch's granted startup
  claim is the release proof: a standing claim would refuse the
  start, fencing the successor;
- stages a reader-less FIFO at a probe peer's sink temporary so its
  graceful flush waits, signals once, requires the probe still runs,
  signals again, and requires prompt exit with status 143 (128+SIGTERM)
  — the second signal's escape hatch a stalled sink must not close —
  then restores the pair's launch roles (duty active, standby
  tracking) for the next leg.

Usage:

    graceful_shutdown.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `graceful-shutdown-digest <sha256>` line prints — the
check runs two passes and compares them
(`graceful-shutdown-nondeterministic`). A contract violation reports
`graceful-shutdown: …` lines on stderr and exits 1 — the check's
`graceful-shutdown-failed`. `--tamper lost-flush` removes the duty's
persisted checkpoint at the restart point — the relaunch cold-starts
and the leg's resume assertions name the loss; `--tamper held-claim`
stages a live foreign writer claim on the plant before the relaunch
— the conditional startup grant refuses it, and the leg names the
fencing a dead claim would visit on the successor; each doctored
pass must exit nonzero carrying its evidence.
"""

import argparse
import json
import os
import re
import signal
import stat
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored cases: a state file gone missing at the restart point,
# and a live foreign claim held across the relaunch, must each
# surface the named diagnostic — never a silently unrestarted or
# silently fenced pass.
LEG = {
    "order": 165,
    "title": "the graceful-shutdown leg",
    "passes": "graceful-shutdown",
    "tampers": [
        {
            "name": "lost-flush",
            "passed": "a lost-flush passed the graceful-shutdown leg",
            "missed": "the lost-flush case did not report its named diagnostic",
            "evidence": ["never reported a resume"],
        },
        {
            "name": "held-claim",
            "passed": "a held-claim passed the graceful-shutdown leg",
            "missed": "the held-claim case did not report its named diagnostic",
            "evidence": ["graceful-shutdown-failed", "claim"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each window runs, the stop bound on the signaled
# exit — the binary contract is ten seconds; the harness's delivery
# plus the poll cadence ride above — and the prompt bound on the
# second signal's forced exit. The actor the leg's receipted
# submissions declare, and the foreign owner token the held-claim
# tamper stages (no member ever pins it).
CONVERGE_TICKS = 4
RECONVERGE_TICKS = 4
STOP_BOUND = 15
PROMPT_BOUND = 5
STALL_OBSERVE = 1.5
ACTOR = "ci-graceful-shutdown"
FOREIGN_TOKEN = 909_001


def wait_exit(process, bound):
    """The process's exit code once it stops inside `bound` seconds —
    None while it still runs."""
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return process.returncode
        time.sleep(0.05)
    return None


def stderr_tail(process):
    """The remaining stderr a spawned peer had not yet reported — the
    startup preamble reader leaves the pipe open, so the run's last
    words (the graceful report among them) read here after exit."""
    try:
        return process.stderr.read() or ""
    except (OSError, ValueError):
        return ""


def terminate(process, failures, what):
    """Deliver SIGTERM to a spawned peer — the harness form of the
    runner's `docker kill --signal=SIGTERM`, with no SIGKILL
    follow-up — and require the graceful exit inside the stop bound:
    exit 0 with the graceful-shutdown marker on stderr. Returns the
    tail for the digest."""
    process.send_signal(signal.SIGTERM)
    code = wait_exit(process, STOP_BOUND)
    tail = stderr_tail(process)
    if code is None:
        failures.append(
            f"{what} never exited within {STOP_BOUND}s of SIGTERM — "
            "the scan loop did not stop at its boundary"
        )
        raise Abort
    if code != 0 or "graceful shutdown" not in tail:
        failures.append(
            f"{what} exited {code} without the graceful shutdown "
            f"(marker {'present' if 'graceful shutdown' in tail else 'absent'}) — "
            "expected exit 0 naming the graceful shutdown"
        )
        raise Abort
    return tail


def declared_command(url, failures):
    """One `(command,)` the peer's served registry declares natively —
    the `invoke` the leg submits."""
    schema = pair.get(f"{url}/schema", "GET /schema", failures)
    declared = pair.declared_command(schema)
    if declared is None:
        failures.append(
            "the served registry declares no command — the leg's "
            "receipted path has nothing to exercise"
        )
        raise Abort
    component, spec = declared
    return {
        "invoke": {
            "component": component,
            "command": spec["name"],
            "arguments": simulate.command_arguments(spec),
        }
    }


def submit(url, command, failures):
    """`POST /command` asserting an accepted receipt; returns it."""
    status, receipt = pair.request(
        f"{url}/command", {"command": command, "actor": ACTOR}
    )
    if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
        failures.append(
            f"the declared command answered {status} {receipt}, "
            "expected an accepted receipt"
        )
        raise Abort
    return receipt


def applied(receipts, command):
    """The applied receipts one submitted command settled."""
    return [
        entry
        for entry in receipts
        if entry.get("command") == command
        and simulate.receipt_outcome(entry) == "applied"
    ]


def role(url, failures):
    """The peer's served RoleReport."""
    return pair.get(f"{url}/role", "GET /role", failures)


def tracking(report):
    """Whether a RoleReport reads `standby` under `tracking` sync."""
    sync = report.get("sync")
    return report.get("role") == "standby" and (
        isinstance(sync, dict) and "tracking" in sync
    )


def impede_sink(directory, failures):
    """Stage a reader-less FIFO at the sink's write-then-rename
    temporary sibling — the stalled mount the second-signal probe
    parks its graceful flush in. A capture's regular temporary in
    flight is retried until its rename clears the path."""
    tmp = os.path.join(directory, "state.json.tmp")
    deadline = time.monotonic() + 10
    while True:
        try:
            os.mkfifo(tmp)
            return
        except FileExistsError:
            if stat.S_ISFIFO(os.lstat(tmp).st_mode):
                return
            if time.monotonic() > deadline:
                failures.append(
                    "a regular state.json.tmp never cleared for "
                    "the stall"
                )
                raise Abort
            time.sleep(0.02)


def graceful_shutdown_pass(args, tamper):
    """The graceful-shutdown run: converge, command, SIGTERM the duty,
    resume it, reconverge, and prove the second signal's prompt exit
    on a stalled probe. Returns `(digest_entries, evidence,
    failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "graceful-shutdown leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    probe = None
    probe_dir = None
    foreign = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_files = rig.duty_files
        state_file = duty_files.get("state_file")
        if state_file is None:
            raise Abort(
                "the manifest's duty declares no state_file — the "
                "graceful-shutdown leg has nothing to exercise"
            )

        # Phase 1 — convergence, then a kind-declared command
        # receipted `accepted` on the owner settling `applied` into
        # both peers' adopted log: the pre-stop run leaves the
        # receipts the resume must carry.
        converged = rig.converge(failures)
        owner = converged["owner"]
        evidence["converged"] = converged["ticks"][-1]
        command = declared_command(duty_url, failures)
        submit(duty_url, command, failures)
        _tracked, owner = rig.tick(standby_url, duty_url, failures)
        for _ in range(5):
            if tracking(role(standby_url, failures)):
                break
            pair.scan(duty_url, failures)
            _tracked, owner = rig.tick(standby_url, duty_url, failures)
        else:
            failures.append(
                "the tracking peer never reconverged after the "
                "command's settling tick"
            )
            raise Abort
        receipts = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
        if not applied(receipts, command):
            failures.append(
                "the declared command never settled applied into "
                "the receipt log"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "command": command["invoke"]["command"],
                "receipts": receipts,
            }
        )

        # Phase 2 — the graceful signal: SIGTERM to the field-owning
        # peer with no SIGKILL follow-up, so the process's own
        # shutdown path is what the leg observes.
        stopped = pair.get(f"{duty_url}/snapshot", "GET /snapshot", failures)
        served_tick = stopped["tick"]
        tail = terminate(rig.duty, failures, "the field-owning duty")
        evidence["stopped_tick"] = served_tick
        try:
            with open(state_file) as handle:
                checkpoint = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            failures.append(
                f"the duty's state file does not hold the flushed "
                f"checkpoint: {error}"
            )
            raise Abort
        persisted = checkpoint.get("tick")
        evidence["persisted_tick"] = persisted
        if persisted != served_tick:
            failures.append(
                f"the duty's state file persisted tick {persisted} "
                f"while the run stood at {served_tick} — the flush "
                "lost the stopped run"
            )
            raise Abort
        if checkpoint.get("model_fingerprint") != rig.fingerprint:
            failures.append(
                "the duty's state file carries fingerprint "
                f"{checkpoint.get('model_fingerprint')}, the manifest "
                f"declares {rig.fingerprint}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "stop",
                "served_tick": served_tick,
                "persisted_tick": persisted,
                "exit": 0,
                "graceful": "graceful shutdown" in tail,
            }
        )

        # Phase 3 — the handover: relaunch the duty onto its declared
        # files. The granted startup claim is the release proof — a
        # standing claim would refuse the start, fencing the
        # successor — and the resumed tick with the reconverged pair
        # is the continuity proof. The relaunch rebinds the duty's
        # own monitor port — the standby's configured tracking
        # target, which no relaunch may move — so the pair's wiring
        # survives the restart exactly as a stable deployment
        # address would.
        if tamper == "lost-flush":
            os.remove(state_file)
        if tamper == "held-claim":
            foreign = simulate.PlantClient(rig.plant_addr)
            answer = foreign.request(
                {"op": "claim_writer", "owner": FOREIGN_TOKEN}
            )
            if answer.get("result") not in ("done", "claimed_shared"):
                failures.append(
                    "the held-claim tamper could not stage its live "
                    f"foreign writer: {answer}"
                )
                raise Abort
        preamble = []
        old_duty_url = rig.duty_url
        old_port = old_duty_url.rsplit(":", 1)[1]
        rig.duty, duty_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            None,
            rig.duty_files,
            listen=f"127.0.0.1:{old_port}",
            pair_token=pair.PAIR_TOKEN,
        )
        rig.duty_url = duty_url
        if duty_url is None:
            detail = "; ".join(preamble[-2:]) or "no diagnostic"
            failures.append(
                "graceful-shutdown-failed: the relaunched duty exited "
                f"at startup: {detail} — a held field claim refuses "
                "the conditional grant, fencing the successor the "
                "graceful release hands the field to"
            )
            raise Abort
        if duty_url != old_duty_url:
            failures.append(
                f"the relaunched duty bound {duty_url}, its tracking "
                f"peer's configured source is {old_duty_url} — the "
                "restart moved the pair's wiring"
            )
            raise Abort
        line = next(
            (line for line in preamble if "resumed from state file" in line),
            None,
        )
        if line is None:
            failures.append(
                "the relaunched duty never reported a resume — its "
                f"cold start silently abandons the persisted run at "
                f"tick {persisted}"
            )
            raise Abort
        match = re.search(r"at tick (\d+)", line)
        resumed = int(match.group(1)) if match else None
        evidence["resumed_tick"] = resumed
        if resumed != persisted:
            failures.append(
                f"the relaunch resumed at tick {resumed}, the state "
                f"file persisted {persisted}"
            )
            raise Abort
        snapshot = pair.get(
            f"{duty_url}/snapshot", "GET /snapshot", failures
        )
        if snapshot["tick"] != persisted:
            failures.append(
                f"the resumed duty reports tick {snapshot['tick']}, "
                f"the persisted tick is {persisted}"
            )
            raise Abort
        receipts_duty = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts:
            failures.append(
                "the resumed duty's receipt log diverged from the "
                "pre-stop log — the adopted audit is not one log"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "resume",
                "resumed_tick": resumed,
                "receipts": receipts_duty,
            }
        )

        # Phase 4 — reconvergence with the launch roles: the duty
        # active again, the standby tracking on it, images identical.
        reconverged = rig.converge(failures, count=RECONVERGE_TICKS)
        evidence["reconverged"] = reconverged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "reconverge",
                "ticks": reconverged["ticks"],
            }
        )

        # Phase 5 — the second signal past a stalled flush: a probe
        # standby persisting at a FIFO-impeded sink parks its graceful
        # flush on the first SIGTERM and exits promptly with -SIGTERM
        # on the second — the escape hatch a stalled sink must not
        # close. The probe tracks the duty without ever owning the
        # field, so the pair's run is undisturbed.
        probe_dir = os.path.join(rig.scratch, "probe")
        os.makedirs(probe_dir, exist_ok=True)
        probe_files = {
            "state_file": os.path.join(probe_dir, "state.json"),
            "journal_file": None,
            "history_file": None,
        }
        probe, probe_url, probe_preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            rig.duty_url.removeprefix("http://"),
            probe_files,
            pair_token=pair.PAIR_TOKEN,
        )
        if probe_url is None:
            detail = "; ".join(probe_preamble[-2:]) or "no diagnostic"
            failures.append(
                f"the probe standby exited at startup: {detail}"
            )
            raise Abort
        pair.scan(probe_url, failures)
        pair.scan(probe_url, failures)
        impede_sink(probe_dir, failures)
        probe.send_signal(signal.SIGTERM)
        time.sleep(STALL_OBSERVE)
        if probe.poll() is not None:
            failures.append(
                f"the first signal exited the impeded probe with "
                f"{probe.returncode} — the flush never waited"
            )
            raise Abort
        probe.send_signal(signal.SIGTERM)
        code = wait_exit(probe, PROMPT_BOUND)
        tail = stderr_tail(probe)
        if code is None:
            failures.append(
                "the second signal never forced the impeded probe's "
                "prompt exit within the bound"
            )
            raise Abort
        # The forced exit is the conventional 128+signo status the
        # handler exits with (143 for SIGTERM) — a prompt process
        # exit, never the signal death (-15) an unhandled SIGTERM
        # reports.
        if code != 128 + signal.SIGTERM:
            failures.append(
                f"the second signal exited {code}, expected the "
                f"prompt {128 + signal.SIGTERM}"
            )
            raise Abort
        digest_entries.append(
            {"phase": "second-signal", "exit": code}
        )
        probe = None
        evidence["final_tick"] = reconverged["owner"]["tick"]
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if foreign is not None:
            foreign.close()
        if probe is not None:
            pair.stop(probe)
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
        choices=["lost-flush", "held-claim"],
        help="doctor the restart point — the pass must fail naming "
        "the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = graceful_shutdown_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"graceful-shutdown: {line}")
        return 1
    for failure in failures:
        eprint(f"graceful-shutdown: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"graceful-shutdown: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored restart"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"graceful-shutdown-digest {digest} — stopped at tick "
        f"{evidence['stopped_tick']}, resumed at tick "
        f"{evidence['resumed_tick']}, tracking again by tick "
        f"{evidence['reconverged']}, run continued to tick "
        f"{evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
