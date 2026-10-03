#!/usr/bin/env python3
"""The shared-state-file refusal leg for the reference plant — the
consumer-side proof that the checkpoint's single-writer claim holds at
the declaration the customer actually deploys, not just at raw argv
(WW-ENG-003, WW-LCM-001 — the
`state-file-shared-between-processes-not-detected` finding), mirrored
at the customer boundary alongside the qa rig's shared-state-file
scenario.

A checkpoint carries one run's state — its tick domain, its receipt
log, its component state, the generation naming the run that wrote it —
so `--state-file` is single-writer, the same contract the two append
sinks declare: an exclusive advisory lock on the path's `.lock` sidecar
is taken before the resume read and held for the process's lifetime.
Two live controllers on one checkpoint file is not a torn document —
the write-then-rename keeps every read whole — but silent state
confusion: their saves interleave, the file carries whichever run
renamed last, and the restart that resumes it adopts the other run's
tick domain, receipts, and component state under a model fingerprint
that matches any same-model writer. The lock turns that into a named
startup refusal, so the second peer exits before it can write at all.

The reference deployment declares the *same* container path on both
members — `/var/tmp/state.json` on the field owner and on its standby
— which is correct precisely because the rig definition keeps a
separate named volume per controller there (`ctrl-a-data` /
`ctrl-b-data`, the reason the release contract records for the split).
The misconfiguration a customer project ships is therefore not in the
manifest's spellings but in the rig definition behind them: one
checkpoint volume reachable from both members, the pair's two declared
`state_file` paths resolving to one file through two mount points. A
copy-pasted volume line is all it takes, and `deploy/manifest.json`
reads exactly as it did before — the deploy stage's screen can only
compare one member's three declared paths against that member's own
mounts, so nothing short of a launch catches this. The run:

- converges the manifest-declared pair on its own unmodified rig
  definition — the field owner `active`, the declared standby
  `tracking` it — each member's declared persistence instantiated at
  the rig's own per-member scratch, the separate volumes the checked-in
  definition declares;
- proves that distinct-path pair settles, persists, and resumes: each
  member's declared checkpoint holds its own run's state at the tick
  its run reached, the two declared checkpoints are two files, and the
  pair relaunched onto those same files — the writer lock released
  with the dead holders — reports each member's own resume at its own
  persisted tick and settles again;
- doctors the deployment on a scratch rig tree, the checked-in
  manifest and compose pristine: the declared standby gains a mount on
  the field owner's checkpoint volume and its declared `state_file`
  follows the `--state-file` flag onto it, so both members' declared
  checkpoints resolve to one file while each member's append sinks keep
  their own volume — the alias under test is the checkpoint's, alone.
  The deploy stage's own screen runs on the doctored pair as the
  corroborating evidence of which surface the contract can live on;
- deploys the doctored pair on its declared launch shape and asserts
  the second writer is refused at startup: it must exit nonzero before
  reporting a listener, naming the writer-lock conflict, the shared
  path, and the `.lock` sidecar the claim rides;
- asserts the holder is undisturbed and the file stays the holder's:
  the field owner keeps scanning across the refusal window, its role
  stays `active` with the field's write-ownership claim held on it, its
  served journal gains no role walk from a seat that never served, and
  the shared checkpoint keeps the holder's own tick and generation —
  never a foreign run's state under the fingerprint that would let a
  restart adopt it.

The contract postdates older release lines and the pinned release may
precede it: a second live writer that reaches its own monitor and
persists beside the holder — the pre-#1341 shape, where both saves
interleave and the restart adopts whichever wrote last — or one whose
launch answers an unrecognized `--state-file` flag, is reported
`shared-state-file-refusal-digest inconclusive` rather than asserted.
An exit naming no writer-lock conflict, or one that names the conflict
without the path it refused, is a failure and not an inconclusive: the
contract demands the refusal name what it refused. A deploy-stage
screen that refuses the doctored pair statically is reported
inconclusive too — the shared volume would then never reach a launch,
and this leg's launch verdict would no longer be reachable evidence.

Usage:

    shared_state_file_refusal.py --plant-server PATH \
        --controller PATH --model model/plant.json \
        --dynamics model/dynamics.json --scenario ci/scenario.json \
        --manifest deploy/manifest.json

On success one `shared-state-file-refusal-digest <sha256>` line prints
— the check runs two passes and compares them
(`shared-state-file-refusal-nondeterministic`). A contract violation
reports `shared-state-file-refusal: …` lines on stderr and exits 1 —
the check's `shared-state-file-refusal-failed`. `--tamper
expect-serving-refusal` doctors the leg's own expectation — the second
writer refused by name AND persisting beside its holder, a disposition
no release can honestly produce — so the leg proves its
refusal-versus-serving classification fires rather than passing an
unexercised contract.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored case.
# The doctored case: a leg asserting the shared checkpoint is refused
# by name AND keeps persisting beside its holder — an impossible
# disposition — must surface the named diagnostic rather than passing
# an unexercised contract.
LEG = {
    # The next free slot after the legs origin/main added past the
    # convergence-gated reclaim one (770) — the pending-serving bound
    # took the 780 this leg first claimed, so the shared-state-file
    # refusal follows it — the stage runs the legs in this order and
    # no two may share one.
    "order": 790,
    "title": "the shared-state-file refusal leg",
    "passes": "shared-state-file-refusal",
    "tampers": [
        {
            "name": "expect-serving-refusal",
            "passed": "an expect-serving-refusal case passed the shared-state-file-refusal leg",
            "missed": "the expect-serving-refusal case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the shared checkpoint refused and persisting beside its holder"
            ],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the checkpoint's single-writer
    claim this leg exercises — or the doctored declaration never
    reaches a launch. Carried as `(reason, detail)`: `reason` the
    stable phrase the `inconclusive` digest line prints (two passes
    must share it), `detail` the run's own verdicts, reported on stderr
    only, where runner-assigned paths belong. A second live writer that
    reached its own monitor is the pre-#1341 shape the contract closed
    — reported here, never as a product failure of a release that
    simply predates it."""


# The refusal vocabulary the second writer's own startup record must
# carry: the conflict itself — the writer lock a live process already
# holds — and the sidecar the claim rides, `<path>.lock`, a sibling the
# checkpoint's write-then-rename never replaces. A refusal naming
# neither names no path, so an operator reading the exit cannot tell
# which file to move.
LOCK_MARK = "writer lock"
LOCK_SIDECAR = ".lock"

# The driven scans the holder runs across the refusal window — a train
# long enough that "it keeps scanning" is a cadence rather than a
# single tick, each one's answer attesting the shared checkpoint's
# durability behind it.
HOLDER_TICKS = 3

# The doctored expectation's stable evidence prefix — every failure
# the tampered pass records carries it, so the check's negative case
# finds it whether the honest run refused the alias or a predating
# release offered the case no evidence at all.
TAMPER_EVIDENCE = (
    "the doctored expectation wanted the shared checkpoint refused "
    "and persisting beside its holder"
)


def declared_members(document):
    """The manifest's `(field owner, declared standby)` entries — the
    field owner the one member without a `standby` field and the
    single member carrying one. Raises Abort where the manifest
    declares no such pair."""
    standbys = [
        entry for entry in document.get("controllers", [])
        if "standby" in entry
    ]
    duties = [
        entry for entry in document.get("controllers", [])
        if "standby" not in entry
    ]
    if len(standbys) != 1 or not duties:
        raise Abort(
            "the manifest declares no single standby pair — the "
            "shared-state-file-refusal leg has nothing to exercise"
        )
    return duties[0], standbys[0]


def service_block(compose, name):
    """The `name:` service's own block of a compose declaration — the
    rig definition's two-space service keys, so the doctoring stays
    keyed on the manifest's member names rather than on the reference
    plant's demonstration labels."""
    lines = compose.splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        if line.rstrip("\n") == f"  {name}:":
            start = index + 1
            break
    if start is None:
        raise Abort(
            f"the rig definition declares no {name} service — the "
            "doctored deployment lost its member"
        )
    end = len(lines)
    for index in range(start, len(lines)):
        line = lines[index]
        # A sibling service key sits at the two-space indent its own
        # children are nested past; anything deeper belongs to the
        # block.
        if (
            line.startswith("  ")
            and not line.startswith("   ")
            and line.strip()
        ):
            end = index
            break
    return start, end, lines


def volume_mount(line):
    """A `- source:target[:mode]` volume line's source and target, or
    None for a line that is not a short-syntax volume mount."""
    stripped = line.strip()
    if not stripped.startswith("- "):
        return None
    parts = stripped[2:].split(":")
    if len(parts) < 2 or not parts[0] or not parts[1].startswith("/"):
        return None
    return parts[0], parts[1]


def persistence_mount(start, end, lines, target):
    """The index of the rw mount line inside a service block whose
    target is `target` — the named volume a member's declared
    persistence lands on."""
    for index in range(start, end):
        mount = volume_mount(lines[index])
        if mount is not None and mount[1] == target:
            return index
    raise Abort(
        f"the rig definition mounts nothing at {target} where the "
        "declared persistence paths live — the doctored deployment "
        "lost its durable storage"
    )


def doctor(args, scratch):
    """The doctored deployment: a scratch rig tree — the copied
    manifest and the copied compose definition beside the copied
    `ci/deploy_rig.py` that screens them, exactly the tree
    `ci/check.sh`'s deploy stage materializes for its own divergence
    cases — in which the declared standby reaches the field owner's
    checkpoint volume and its declared `state_file` (and the
    `--state-file` flag carrying it) name the file there, while each
    member's append sinks keep their own volume. Returns the record
    the leg deploys from: the doctored manifest path, the standby's
    doctored declared checkpoint path, and the deploy stage's screen
    verdict on the pair with its output."""
    with open(args.manifest) as handle:
        document = json.load(handle)
    duty, standby = declared_members(document)
    if not duty.get("state_file") or not standby.get("state_file"):
        raise Inconclusive(
            "the manifest's pair declares no state_file on both "
            "members — the consumer harness admits no checkpoint to "
            "share",
            f"the declared paths are {duty.get('state_file')!r} and "
            f"{standby.get('state_file')!r}",
        )
    ci_dir = os.path.dirname(_HERE)
    root = os.path.dirname(ci_dir)
    rig_dir = os.path.join(scratch, "rig-shared-state")
    for sub in ("ci", "deploy", "model"):
        os.makedirs(os.path.join(rig_dir, sub))
    shutil.copyfile(
        os.path.join(ci_dir, "deploy_rig.py"),
        os.path.join(rig_dir, "ci", "deploy_rig.py"),
    )
    for artifact in ("plant.json", "dynamics.json"):
        shutil.copyfile(
            os.path.join(root, "model", artifact),
            os.path.join(rig_dir, "model", artifact),
        )

    # The second mount point: a subdirectory of the directory the
    # field owner's declared checkpoint lives in, so the two members
    # reach the one file through two distinct mount points and the
    # screen's innermost-mount rule still resolves each declared path
    # onto a read-write mount.
    directory = os.path.dirname(duty["state_file"]).rstrip("/")
    if not directory:
        raise Inconclusive(
            "the manifest's declared checkpoint carries no directory "
            "for a second mount point to sit in — the consumer harness "
            "admits no shared volume",
            f"the declared path is {duty['state_file']!r}",
        )
    if os.path.dirname(standby["state_file"]).rstrip("/") != directory:
        raise Inconclusive(
            "the manifest's pair declares its checkpoints under "
            "different directories — the consumer harness's shared "
            "volume doctor rides the one mount point both members "
            "persist under",
            f"the declared paths are {duty['state_file']!r} and "
            f"{standby['state_file']!r}",
        )
    mount = f"{directory}/shared-{os.path.basename(directory) or 'data'}"
    aliased = f"{mount}/{os.path.basename(duty['state_file'])}"
    standby["state_file"] = aliased

    with open(os.path.join(rig_dir, "deploy", "manifest.json"), "w") as handle:
        json.dump(document, handle, indent=2)
    with open(os.path.join(root, "deploy", "compose.yaml")) as handle:
        lines = handle.readlines()
    duty_start, duty_end, _ = service_block("".join(lines), duty["name"])
    volume = volume_mount(
        lines[persistence_mount(duty_start, duty_end, lines, directory)]
    )[0]
    start, end, lines = service_block("".join(lines), standby["name"])
    # The standby's own persistence mount keeps the append sinks it
    # backs; the owner's checkpoint volume is added beside it.
    lines.insert(
        persistence_mount(start, end, lines, directory) + 1,
        f"      - {volume}:{mount}\n",
    )
    start, end, lines = service_block("".join(lines), standby["name"])
    try:
        state_flag = lines.index("      - --state-file\n", start, end)
    except ValueError:
        raise Abort(
            f"the rig definition's {standby['name']} invocation carries "
            "no --state-file flag — the doctored deployment lost the "
            "checkpoint under test"
        ) from None
    lines[state_flag + 1] = f"      - {aliased}\n"
    with open(os.path.join(rig_dir, "deploy", "compose.yaml"), "w") as handle:
        handle.writelines(lines)

    proc = subprocess.run(
        [sys.executable, "ci/deploy_rig.py"],
        cwd=rig_dir,
        capture_output=True,
        text=True,
    )
    out = (proc.stdout + proc.stderr).strip()
    if "rig-unverifiable" in out:
        screen = "unverifiable"
    elif proc.returncode != 0:
        screen = "refused"
    else:
        screen = "agrees"
    return {
        "manifest": os.path.join(rig_dir, "deploy", "manifest.json"),
        "standby_state": aliased,
        "screen": screen,
        "screen_out": out,
    }


def launch_shared(args, declared, shared_path):
    """The doctored pair launched with both members' declared
    `--state-file` carrying `shared_path` — the one file the two
    declared checkpoints resolve to through the shared volume, which
    the harness's own instantiation (`pair.persistence_files`, one
    scratch directory per member, the two separate volumes the rig
    definition declares) cannot express. Everything else keeps that
    mapping: the declared plant, the two driven peers, each member's
    own append sinks under its own scratch, the declared
    `failover_budget` arming the standby's self-promotion, and the
    pair's shared tracking secret. Returns the `PairRig` — the
    standby's slot carries the second writer's process, monitor url,
    and startup preamble, whatever became of its launch."""
    rig = pair.PairRig(declared)
    rig.duty_files["state_file"] = shared_path
    rig.standby_files["state_file"] = shared_path
    try:
        rig.plant, rig.plant_addr = pair.spawn_plant(
            args.plant_server, args.model, args.dynamics
        )
        rig.plant_io = simulate.PlantClient(rig.plant_addr)
        rig.duty, rig.duty_url, rig.duty_preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            None,
            rig.duty_files,
            pair_token=pair.PAIR_TOKEN,
        )
        if rig.duty_url is None:
            raise Abort(
                f"the field owner {rig.duty_decl['name']} exited at "
                "startup on the shared checkpoint: "
                f"{'; '.join(rig.duty_preamble) or 'no diagnostic'}"
            )
        rig.standby, rig.standby_url, rig.standby_preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            rig.duty_url.removeprefix("http://"),
            rig.standby_files,
            auto_promote=rig.standby_decl.get("failover_budget"),
            pair_token=pair.PAIR_TOKEN,
        )
    except Exception:
        rig.close()
        raise
    return rig


def classify(rig, shared_path):
    """The second writer's launch verdict. Returns `(verdict, detail)`:

    - "refused" — the launch exited nonzero before reporting a
      listener, naming the writer-lock conflict, the shared path, and
      the `.lock` sidecar the claim rides;
    - "serving" — the launch reached its own monitor: the pre-#1341
      shape, where both members persist into one checkpoint;
    - "unsupported" — the exit names an unrecognized flag, a release
      predating the persistence vocabulary itself;
    - "exited" — a startup exit naming no writer-lock conflict on the
      shared path.
    """
    if rig.standby_url is not None:
        return "serving", f"reported a listener at {rig.standby_url}"
    detail = " ".join(rig.standby_preamble).strip() or "no diagnostic"
    if "unknown option" in detail or "unrecognized" in detail:
        return "unsupported", detail[:300]
    if (
        LOCK_MARK in detail
        and shared_path in detail
        and shared_path + LOCK_SIDECAR in detail
    ):
        return "refused", detail[:300]
    return "exited", detail[:300]


def checkpoint(path):
    """The run state persisted at `path`, or None where the file is
    absent or does not parse."""
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def persisted(rig, path, name, failures):
    """The run state `name`'s declared checkpoint holds — the durable
    record whose loss is recorded as a failure rather than read as a
    quiet tick."""
    state = checkpoint(path)
    if state is None:
        failures.append(
            f"{name}'s declared checkpoint does not hold a parseable "
            "run state — the --state-file flag was not honored"
        )
        return None
    tick = state.get("tick")
    if not isinstance(tick, int) or tick < 1:
        failures.append(
            f"{name}'s declared checkpoint persists tick {tick!r} — "
            "the run's own progress never reached its durable state "
            "file"
        )
        return None
    if state.get("model_fingerprint") != rig.fingerprint:
        failures.append(
            f"{name}'s declared checkpoint carries fingerprint "
            f"{state.get('model_fingerprint')}, the manifest declares "
            f"{rig.fingerprint}"
        )
    return state


def resume_tick(preamble, what, failures):
    """The tick `what`'s relaunch reported resuming from its own
    declared checkpoint, or None where the launch never claimed one —
    a silent cold start abandoning the persisted run."""
    line = next(
        (line for line in preamble if "resumed from state file" in line),
        None,
    )
    if line is None:
        failures.append(
            f"the relaunched {what} never reported a resume from its "
            "declared checkpoint — its cold start silently abandons "
            "the run its predecessor persisted"
        )
        return None
    match = re.search(r"at tick (\d+)", line)
    return int(match.group(1)) if match else None


def relaunch(rig, args):
    """The declared pair relaunched onto its own declared
    persistence, both members stopped first — the writer lock releases
    with the dead holder's descriptor, the recovery the checkpoint
    exists for and the contract's guard must leave untouched. The
    field owner is launched first so the standby wires at the new
    lifetime's monitor, as the rig definition's own service ordering
    does. The rig's processes, monitor urls, and preambles follow.
    Returns `(duty_preamble, standby_preamble)`."""
    pair.stop(rig.standby)
    pair.stop(rig.duty)
    rig.duty, rig.duty_url, rig.duty_preamble = pair.spawn_peer(
        args.controller,
        args.model,
        args.dt,
        rig.plant_addr,
        None,
        rig.duty_files,
        pair_token=pair.PAIR_TOKEN,
    )
    if rig.duty_url is None:
        raise Abort(
            "the relaunched field owner exited at startup on its own "
            "declared checkpoint: "
            f"{'; '.join(rig.duty_preamble) or 'no diagnostic'}"
        )
    rig.standby, rig.standby_url, rig.standby_preamble = pair.spawn_peer(
        args.controller,
        args.model,
        args.dt,
        rig.plant_addr,
        rig.duty_url.removeprefix("http://"),
        rig.standby_files,
        auto_promote=rig.standby_decl.get("failover_budget"),
        pair_token=pair.PAIR_TOKEN,
    )
    if rig.standby_url is None:
        raise Abort(
            "the relaunched standby exited at startup on its own "
            "declared checkpoint: "
            f"{'; '.join(rig.standby_preamble) or 'no diagnostic'}"
        )
    return rig.duty_preamble, rig.standby_preamble


def holder_health(rig, shared_path, failures, journal_floor):
    """The field owner across the refusal window: `HOLDER_TICKS`
    driven scans, each one's answered tick the shared checkpoint must
    then hold — the holder keeps its own cadence and the one file
    follows it alone, which is the whole claim — beside the role
    report naming `active` with the field's write-ownership claim held
    on it and its served journal read above `journal_floor`: a seat
    that occupied a pair position without serving one would journal
    its own role walk there. Returns `(owner, role)` — the last scan's
    snapshot and the role report."""
    owner = None
    for _ in range(HOLDER_TICKS):
        owner = pair.scan(rig.duty_url, failures)
        served = pair.get(
            f"{rig.duty_url}/checkpoint", "GET /checkpoint", failures
        )
        held = checkpoint(shared_path) or {}
        if held.get("tick") != owner["tick"]:
            failures.append(
                f"the shared checkpoint persisted tick {held.get('tick')} "
                f"while its holder stood at {owner['tick']} — the file "
                "carries another run's tick domain"
            )
        for field in ("generation", "model_fingerprint"):
            if held.get(field) != served.get(field):
                failures.append(
                    f"the shared checkpoint carries {field} "
                    f"{held.get(field)!r} while its holder serves "
                    f"{served.get(field)!r} — a foreign run's state "
                    "reached the file the holder owns"
                )
    role = pair.get(f"{rig.duty_url}/role", "GET /role", failures)
    if role.get("role") != "active" or role.get("field_claim") != "held":
        failures.append(
            f"the field owner reports {role.get('role')!r} with claim "
            f"{role.get('field_claim')!r} across the refused second "
            "writer, expected active/held — the refusal disturbed the "
            "holder"
        )
    walked = [
        entry
        for entry in pair.get(
            f"{rig.duty_url}/journal", "GET /journal", failures
        )[journal_floor:]
        if "role_changed" in entry.get("event", {})
    ]
    if walked:
        failures.append(
            f"the field owner's served journal gained the role walks "
            f"{walked} — the refused seat occupied a pair position it "
            "never served"
        )
    return owner, role


def shared_state_file_refusal_pass(args, tamper):
    """The shared-state-file run: converge the declared pair on its
    own rig definition, prove its distinct checkpoints settle,
    persist, and resume across a restart, then deploy the doctored
    pair whose two members share one checkpoint file and assert the
    second writer is refused at startup by name with the holder
    undisturbed. Returns `(digest_entries, evidence, failures)`;
    raises `Inconclusive` where the pinned release predates the
    contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "shared-state-file-refusal leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    if not duty_decl.get("state_file") or not standby_decl.get("state_file"):
        raise Inconclusive(
            "the manifest's pair declares no state_file on both members "
            "— the consumer harness admits no checkpoint to share",
            f"the declared paths are {duty_decl.get('state_file')!r} and "
            f"{standby_decl.get('state_file')!r}",
        )
    digest_entries, evidence, failures = [], {}, []
    scratch = tempfile.mkdtemp(prefix="dcs-shared-state-")
    rig = None
    try:
        # Phase 1 — the distinct-path pair: the checked-in rig
        # definition's separate volumes, the declared pair settled and
        # each member's declared checkpoint holding its own run's state.
        rig = pair.launch_pair(args, declared)
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "control",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )
        paths = {
            duty_decl["name"]: rig.duty_files["state_file"],
            standby_decl["name"]: rig.standby_files["state_file"],
        }
        if len(set(paths.values())) != len(paths):
            failures.append(
                "the declared pair's checkpoints instantiate to one "
                f"file ({sorted(set(paths.values()))}) — the harness's "
                "per-member scratch must keep the two declared "
                "checkpoints apart"
            )
            raise Abort
        held = {}
        for name, path in paths.items():
            state = persisted(rig, path, name, failures)
            if state is not None:
                held[name] = state["tick"]
        if len(held) != len(paths):
            raise Abort
        digest_entries.append(
            {
                "phase": "control-persisted",
                "files": len(set(paths.values())),
                "ticks": held,
            }
        )

        # Phase 2 — the same pair resumes across the restart: each
        # member relaunched onto its own declared checkpoint reports
        # its own resume at its own persisted tick — the guard leaves
        # the recovery the checkpoint exists for intact — and the pair
        # settles again on the continuing run.
        duty_preamble, standby_preamble = relaunch(rig, args)
        resumed = {
            duty_decl["name"]: resume_tick(
                duty_preamble, "field owner", failures
            ),
            standby_decl["name"]: resume_tick(
                standby_preamble, "standby", failures
            ),
        }
        if failures:
            raise Abort
        for name, tick in resumed.items():
            if tick != held[name]:
                failures.append(
                    f"the relaunched {name} resumed at tick {tick}, "
                    f"its declared checkpoint persisted {held[name]}"
                )
                raise Abort
        settled = rig.converge(failures)
        digest_entries.append(
            {
                "phase": "control-resume",
                "resumed": resumed,
                "ticks": settled["ticks"],
            }
        )
        evidence["resumed_to"] = settled["ticks"][-1]
        rig.close()
        rig = None

        # Phase 3 — the doctored declaration: the declared standby
        # reaches the field owner's checkpoint volume, so the pair's
        # two declared checkpoints resolve to one file. The deploy
        # stage's own screen runs on the doctored pair first: a
        # refusal there means the shared volume never reaches a launch
        # and this leg's launch verdict is no longer reachable
        # evidence.
        doctored = doctor(args, scratch)
        if doctored["screen"] == "refused":
            raise Inconclusive(
                "the deploy stage now refuses the shared checkpoint "
                "volume statically — the declaration never reaches a "
                "launch",
                doctored["screen_out"],
            )
        if doctored["screen"] == "unverifiable":
            raise Inconclusive(
                "the consumer harness cannot screen the doctored rig — "
                "no parser runs ci/deploy_rig.py",
                doctored["screen_out"],
            )
        aliased = pair.manifest_pair(doctored["manifest"])
        if aliased is None:
            raise Abort(
                "the doctored declaration declares no standby pair — "
                "the doctor lost its deployment"
            )
        shared_root = os.path.join(scratch, "shared-checkpoint")
        os.makedirs(shared_root, exist_ok=True)
        shared_path = os.path.join(
            shared_root, os.path.basename(doctored["standby_state"])
        )

        # Phase 4 — the second writer: the doctored pair launched with
        # both members' `--state-file` carrying that one path. The
        # field owner takes the claim before its resume read and keeps
        # it for its lifetime; the standby must be refused at startup,
        # before it can write at all.
        rig = launch_shared(args, aliased, shared_path)
        verdict, detail = classify(rig, shared_path)
        if tamper is not None:
            # The doctored expectation — refused by name AND
            # persisting beside the holder, the defect shape the
            # contract closed. Every observed verdict fails it, naming
            # what the launch actually did.
            failures.append(
                f"{TAMPER_EVIDENCE} — the shared-checkpoint standby "
                + {
                    "refused": f"exited naming the writer lock: {detail}",
                    "serving": f"reported a listener and persists beside the holder: {detail}",
                    "unsupported": f"exited on an unrecognized flag: {detail}",
                    "exited": f"exited without naming the writer lock: {detail}",
                }[verdict]
            )
            raise Abort
        if verdict == "serving":
            raise Inconclusive(
                "the pinned release predates the checkpoint's "
                "single-writer claim — the second writer persisted "
                "beside its holder",
                f"the shared-checkpoint standby {detail} and persists "
                f"into the holder's file; the deploy stage's screen "
                f"reports {doctored['screen']}",
            )
        if verdict == "unsupported":
            raise Inconclusive(
                "the pinned release predates the persistence vocabulary "
                "the shared declaration is written on",
                detail,
            )
        if verdict == "exited":
            failures.append(
                "the shared-checkpoint standby exited without naming "
                "the writer-lock conflict and the path it refused — "
                "the contract demands the refusal name what it "
                f"refused: {detail}"
            )
            raise Abort
        if rig.standby.returncode == 0:
            failures.append(
                "the shared-checkpoint standby exited zero — a second "
                "live writer on one checkpoint is a usage error, never "
                "a silent peer"
            )
            raise Abort
        eprint(
            "shared-state-file-refusal: the shared-checkpoint standby "
            f"refused — {detail}"
        )

        # Phase 5 — the holder undisturbed and the file still the
        # holder's: the field owner keeps scanning across the refusal
        # window with the field's claim on it and no role walk in its
        # journal, and the shared checkpoint keeps its own tick and
        # run generation behind every one of those scans.
        journal_floor = len(
            pair.get(f"{rig.duty_url}/journal", "GET /journal", failures)
        )
        owner, role = holder_health(
            rig, shared_path, failures, journal_floor
        )
        digest_entries.append(
            {
                "phase": "refusal",
                "deploy_screen": doctored["screen"],
                "launched": "refused-by-name",
                "names": ["writer-lock", "shared-path", "lock-sidecar"],
                "holder": {
                    "role": role.get("role"),
                    "claim": role.get("field_claim"),
                    "journal": "no-role-walk",
                    "scans": HOLDER_TICKS,
                    "checkpoint": "holder-only",
                },
            }
        )
        evidence["final_tick"] = owner["tick"]
        if failures:
            raise Abort
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
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
        choices=["expect-serving-refusal"],
        help="doctor the leg's own expectation — the shared "
        "checkpoint must be refused by name AND keep persisting beside "
        "its holder, a disposition no release can honestly produce — "
        "so the pass must fail naming what the launch actually did",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = shared_state_file_refusal_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"shared-state-file-refusal: {TAMPER_EVIDENCE} — an "
                "inconclusive run offers the doctored case no evidence"
            )
            return 1
        # The digest line carries only the stable reason — the run's
        # own verdicts and runner-assigned paths report on stderr,
        # where two identical passes need not share them.
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"shared-state-file-refusal: inconclusive — {detail}")
        print(f"shared-state-file-refusal-digest inconclusive — {reason}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"shared-state-file-refusal: {line}")
        return 1
    for failure in failures:
        eprint(f"shared-state-file-refusal: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"shared-state-file-refusal: the {args.tamper} case "
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
        f"shared-state-file-refusal-digest {digest} — the declared "
        f"pair converged on its own rig definition at tick "
        f"{evidence['converged']}, each member's own checkpoint "
        "settled, persisted, and resumed across the restart to tick "
        f"{evidence['resumed_to']}, the declaration sharing one "
        "checkpoint file across the pair passed the deploy stage's own "
        "screen and was refused at startup naming the writer-lock "
        "conflict, the shared path, and its .lock sidecar, and the "
        "holder kept scanning with the field's claim to tick "
        f"{evidence['final_tick']} with the shared file carrying only "
        "its own run's state"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
