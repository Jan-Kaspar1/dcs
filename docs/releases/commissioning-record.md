# Commissioning and handover record

The record a pilot deployment's commissioning handover assembles —
`WW-LCM-002`'s commissioning-and-handover clause, per the owner and
standards evidence in `docs/research/deployment-commissioning.md`.
This is a **recorded artifact set and procedure**, not a new
mechanism: the record is materialized from the deterministic evidence
an existing driven run already produces — no new wire types, no new
persisted formats, no new served surface. The reference plant's
`ci/commissioning.py` leg produces it inside clean CI as part of the
`pair` stage, and the completeness audit is what makes the record a
contract: a run that cannot produce every named artifact fails by
name rather than handing over a partial record.

The record is distinct from the configuration-backup artifact set
(#822): the backup set enumerates what a *restore* needs — the
documents and durable files a deployment must retain — while the
commissioning record enumerates what a *handover* produces — the
witnessed evidence that the deployed pair was checked out, its loops
exercised, its alarm record signed off, and its documents turned over
before acceptance. A backup restores a deployment; this record hands
one over.

## The artifact set

The named set follows the owner practice the research records —
point-to-point I/O checkout, the loop check as a rising-and-falling
span ladder, the rationalized alarm database signed off per alarm,
and document turnover with named contents
(`docs/research/deployment-commissioning.md`, "The commissioning
workflow"):

| Artifact | What the drill materializes |
|---|---|
| `io-checkout` | The I/O checkout record — the plant protocol's `list_points` census audited against the emitted model's declared channel-backed I/O set: every declared point present in the field with its declared direction and value kind, the served image's sample equal to a value the field actually presented at the scan boundary (the field steps after each scan's reads, so live points are checked against the read-side sample and commanded ones against the delivered value), and no undeclared field point appearing. |
| `loop-check` | The loop-check evidence — the declared measurement loop (`level-primary` through the threshold chain) driven at marks across the chain's declared `cutoff..high` span, rising and falling inside the latch thresholds so the excursion exercises the chain's `demand` and the group's `staged` report without leaving a tripped interlock or a latched alarm: each field-side write joined under the field owner's recorded writer-claim token, read back from the field, and served identically by both peers at the next driven tick, the chain's response recorded per mark. Then the declared output loop end to end through the receipted path: `p101-mode` cutting the delivered `p101-cmd` off the group's request, `p101-hand` running the pump so the field census carries the delivered command and the `p101-run` feedback returns it, and the restore returning the pump to group control — every write settling `applied` into the adopted receipt log. |
| `alarm-signoff` | The alarm rationalization sign-off — every managed alarm instance's declared record audited served verbatim on both peers: the `rationalization` block (consequence, required action, and the display/procedure reference) plus the `priority`/`class`/`response_ticks` codes, the single declaration riding the checkpoint to the tracking peer. The per-alarm test-certificate expectation the owner alarm standards name, answered by the deployed pair's served record rather than a separate document workflow. |
| `documentation-turnover` | The documentation turnover — the deployment's document set digested under its declared paths (`model/plant.json`, `model/dynamics.json`, `deploy/manifest.json`), each peer's checkpoint-stamped `model_fingerprint` held equal to the manifest's recorded fingerprint, each peer's durable `--journal-file` digested over its normalized records, and each `--state-file`'s persisted tick and fingerprint reported. |

## The handover procedure

The record names the procedure the driven run executes, in order:

1. **Converge** — launch the manifest-declared pair on the released
   tooling and drive ticks until the tracking peer's per-scan pull has
   applied the field owner's latest checkpoint and the pair rests
   identical.
2. **I/O checkout** — the field census against the declared channel
   set, point for point.
3. **Loop check** — the measurement ladder and the output-loop
   exercise above.
4. **Alarm sign-off** — the declared record audited on both peers.
5. **Handover** — the documented `demote`/`promote` switch: demote the
   field owner, promote the converged standby, drive the handover
   ticks with the run continuing bumplessly, then restore the pair to
   its declared launch roles — the witnessed changeover a pilot's
   availability clause expects, each peer's served journal carrying
   the role transitions in order.
6. **Documentation turnover** — the document set digested and the
   durable files reported.
7. **Completeness audit** — the assembled record must carry every
   named artifact; a record missing one fails naming it.

The record itself is one normalized JSON evidence document — the
declared set above keyed by artifact name, plus the release, images,
model, dynamics, and peer identities it was produced against and the
procedure list — hashed into the leg's `commissioning-digest`. Two
consecutive runs must produce byte-identical digests: the record is
reproducible evidence, not a narrative.

## Named diagnostics

Reported by the reference plant's `ci/check.sh`:

- `commissioning-failed` — the leg did not hold: any contract
  violation inside the run, reported with the leg's
  `commissioning: …` evidence lines on stderr — a census point missing
  or diverged, a driven mark not served identically, a receipted
  output-loop write unsettled, a served alarm record adrift of the
  declaration, a switch step unanswered, a fingerprint or durable
  file's report diverging, or the completeness audit's
  `the record carries no <artifact> artifact` line.
- `commissioning-nondeterministic` — two passes produced different
  digests.
- `commissioning-unchecked` — a doctored missing-artifact run
  (`--tamper missing-io-checkout`, `missing-loop-check`,
  `missing-alarm-signoff`, or `missing-documentation-turnover`)
  passed without the completeness audit naming the dropped artifact.
