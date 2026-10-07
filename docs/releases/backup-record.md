# Configuration-backup artifact set and rollback drill

The backup set a pilot deployment retains and the restore drill proving
it restores — `WW-LCM-002`'s backup-and-restore clause, per the owner
and standards evidence in
`docs/research/deployment-commissioning.md`. This is a **recorded
artifact set and procedure**, not a new mechanism: the backup replays
existing deployment-supplied paths onto fresh volumes and resumes
through the existing `--state-file` recovery — no new wire types, no
new persisted formats, no new served surface. The reference plant's
`ci/legs/backup_restore.py` leg drills it inside clean CI as part of
the `pair` stage, and the completeness audit is what makes the record
a contract: a backup missing a named artifact fails by name rather
than restoring a partial set.

The record is distinct from the commissioning record
(`docs/releases/commissioning-record.md`): the commissioning record
enumerates what a *handover* produces — the witnessed evidence that
the deployed pair was checked out and handed over — while this record
enumerates what a *restore* needs — the documents and durable files a
deployment must retain. A backup restores a deployment; the
commissioning record hands one over.

## The artifact set

The named set follows the enumerated restore scope the research
records — program and logic files, configuration details, and the
system images among them
(`docs/research/deployment-commissioning.md`, "The backup baseline") —
applied to this platform's deployment-supplied paths (decision 46):

| Artifact | What the drill backs up |
|---|---|
| `model` | The plant-model document the deployment mounts (`model.path`) plus its canonical fingerprint (`model.fingerprint`) — the identity checkpoint negotiation verifies on the wire. |
| `dynamics` | The simulation-dynamics document the plant server merges (`dynamics.path`) plus its recorded fingerprint where the manifest pins one. |
| `state` | Each declared controller's `--state-file` — the restart-recovery checkpoint the run persists every completed cycle, resumed at its persisted tick. |
| `journal` | Each declared controller's `--journal-file` — the durable attributed operator-action record, its `seq` order continuing across the restore's run-boundary marker. |
| `history` | Each declared controller's `--history-file` where the manifest declares one — the durable process-history store, replayed at bind into the bounded served window. |
| `manifest pin` | The deployment manifest itself (`dcs_release`, images, mount paths, fingerprints) — the pin a rollback repins by name. |

The backup record (`backup.json`) binds the set together: the
`dcs_release` pin, the model and dynamics fingerprints, and every
file's sha256. The drill verifies each file against its recorded hash
at audit time, so the set cannot change under its record.

## The backup procedure

The drill backs a running pair up without stopping it:

1. **Converge** — launch the manifest-declared pair on the released
   tooling and drive ticks until the tracking peer rests identical to
   the field owner, then take one receipted operator write so the
   backup has retained state, an applied receipt, and journal
   transitions to keep.
2. **Copy** — copy the model, dynamics, and manifest documents and
   each declared controller's durability files beside the live
   deployment, and record `backup.json`.
3. **Audit** — verify the backup's completeness before any restore
   runs: the recorded `dcs_release` must equal the deployment's, every
   named document must be present, every declared controller must
   carry its state and journal files, and every file must match its
   recorded sha256. A backup missing a named artifact fails naming it.

## The restore procedure

The drill restores the audited set onto fresh volumes:

1. **Wipe** — stop both controllers and delete their persistence
   files. The field server stands throughout: the volumes are the
   deployment's durability mounts, not the field.
2. **Replay** — copy the backup set onto fresh volumes at the
   manifest-declared paths.
3. **Resume** — relaunch the duty controller first onto its restored
   files: it must report its resume at its persisted tick — never a
   silent cold start — serve the pre-wipe receipt log verbatim, and
   replay the pre-wipe journal behind the restart's `run_boundary`
   entry with the durable `seq` order continuing across it. (A
   relaunched launched-active consults the incumbent before its
   startup claim; with the old peers stopped the pull is refused and
   the run journals the `restart_consult` audit beside the boundary.)
4. **Rejoin** — relaunch the tracking peer onto its restored files at
   its own persisted tick, reconverge to `tracking`, and continue the
   run: identical images, one receipt log, roles restored.

Two consecutive drills must produce byte-identical digests: the drill
is reproducible evidence, not a narrative. The digest covers the
record's stable content — the pin, the document fingerprints, the
file names, the persisted and resumed ticks, the receipts, and the
continued run — never the files' raw bytes: the state file embeds the
run's per-boot receipt nonces and the journal file the restart
consult's ephemeral source, so their bytes legitimately differ run to
run while the restored run is identical.

## Named rollback semantics across a repin

Rollback is a named direction with two forms, and the drill proves the
second while the revision leg proves the machinery the first borrows:

- **Compatible repin rollback** — the platform upgrade the reference
  plant's `upgrade` stage drills: only the release pin moves while the
  model fingerprint stands. Rollback repins the manifest to the
  retained prior `dcs_release`, re-resolves, and relaunches onto the
  retained state and journal files: the fingerprint is unchanged, so
  the checkpoint applies directly and the run resumes at its persisted
  tick with receipts and journal order continuing. No revision
  crossing runs — there is no model boundary to carry.
- **Revised-model rollback** — the in-service revision the
  `revision-roll` leg drills. Before the revised peer promotes,
  rollback is aborting the roll: stop the `--revised` peer while the
  old active still owns the field — no state moves. After promotion,
  rollback restores the pre-roll backup set onto fresh volumes by the
  procedure above. Runtime state does not carry backward across the
  revision boundary: the carryover rule is forward-only, and the
  retained old build cannot track the revised line's checkpoints.

## Named diagnostics

Reported by the reference plant's `ci/check.sh`:

- `backup-restore-failed` — the drill did not hold: the backup's
  record disagreed with the deployment, a live durability file was
  absent, a file failed its recorded sha256, a relaunched peer never
  reported its resume or resumed at another tick than the backup
  persisted, the receipt log diverged from the pre-wipe log, the
  served journal did not replay the pre-wipe entries behind the
  run-2 boundary, the durable `seq` order broke across it, or the
  rejoined pair did not reconverge — each reported with the leg's
  `backup-restore: …` evidence lines on stderr.
- `backup-restore-nondeterministic` — two passes produced different
  digests.
- `backup-restore-unchecked` — a doctored backup missing a named
  artifact (`--tamper missing-state-file`, `missing-journal-file`)
  passed the completeness audit without naming it.
