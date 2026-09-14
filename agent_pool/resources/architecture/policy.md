# DCS autonomous architecture review policy

This policy governs the daily architecture reviewer. It is project-owned,
versioned with the supervisor, and takes precedence over the vendored
reference workflows in `references/`. There is no user in the loop: no
candidate-selection questions, no design questionnaires, no approval gates,
no interactive reports. A review's output is a record; accepted work flows
through the existing planner, workers, CI, and merge supervisor.

## Desired outcomes

- **Deep modules.** A deep module hides meaningful behavior — state
  transitions, ordering, error handling — behind an interface that requires
  less knowledge from callers than the behavior it provides. Depth is
  leverage: more capability per unit of interface a caller must learn.
- **Justified seams.** A seam is justified by concrete variation (two real
  adapters, e.g. simulated and physical I/O) or a meaningful testing need.
  Adding wrappers, traits, or indirection that does not reduce caller
  complexity is not an improvement.
- **Consistent domain naming.** Domain concepts have canonical names used
  consistently across Rust types, Python tooling, plant-model fields,
  monitoring wire contracts, tests, documentation, and UI terminology.
  Language-specific spelling conventions (Rust `CamelCase` types vs JSON
  `snake_case` fields) are preserved; the underlying concept name is what
  must be shared.

## Evaluating a candidate

Apply the **deletion test**: if deleting the candidate module would spread
its complexity across callers, it earns its keep; if complexity simply
vanishes, it was a pass-through. For every proposal, state what complexity
disappears from callers — not what gets tidier inside.

Do not reward, by themselves: more traits, fewer files, renamed types,
smaller line counts, or symmetry. A candidate must state which outcome it
advances — depth, naming consistency, or both.

Prioritize hotspots: areas with recent changes, repeated defects, difficult
worker tasks, or upcoming roadmap work. When no hotspot exists, the
scheduled **coverage focus** names a quieter area to inspect, so reviews
over time cover the whole repository including naming.

A **seam proposal** requires naming the real variation it serves. A single
conceivable adapter is a hypothetical seam — do not propose it.

## Naming policy

- Bootstrap vocabulary from existing code and documented meaning. Prefer
  established terms whose meaning is already precise; do not invent a new
  vocabulary every day.
- Distinct concepts must not be collapsed merely because their names look
  similar. Ambiguous terms that produce incompatible representations or
  contracts are naming candidates.
- For each naming change record: old names mapped to the canonical term,
  the affected layers (code, model fields, wire contract, monitoring, UI,
  docs), and public compatibility implications.
- Migrate internal references and documentation together in one change.
- Preserve persisted model formats and public wire names unless an accepted
  versioning or migration decision permits the change. When a rename would
  break a persisted or wire contract, propose it with an explicit
  compatibility treatment or defer it.
- Resolved terms belong in `CONTEXT.md` — a glossary, not a second
  specification; create it lazily when the first term is resolved.
  Technical decisions continue to be recorded in `docs/architecture.md`;
  do not introduce a competing ADR history.
- The reviewer proposes glossary/decision edits inside its report; workers
  apply accepted edits in their assigned implementation work. The reviewer
  never edits repository files.

## Constraints and deferral

- Existing decisions in `docs/architecture.md` and observable behavior are
  constraints. Reopening a decision requires new evidence, recorded as a
  decision conflict on the candidate.
- If a candidate requires inventing product semantics, contradicts an
  unresolved architectural decision, or depends on information only a human
  holds, the planner will defer it. Make deferral easy: state precisely what
  is unresolved and what evidence would revisit it.
- Speculative work — improvements whose caller benefit cannot be
  demonstrated on current code — is rejected, not deferred.

## Reports and assessment

- The report is the only output. Invalid or missing output is an
  inconclusive run, never an accepted review.
- A completed review with zero candidates is a successful outcome.
- Each run also assesses previously merged improvements: compare actual
  before/after callers and migrated names against the stated benefit. An
  improvement is not confirmed because CI passed; it is confirmed because
  callers got simpler or names became consistent while compatibility held.
  A failed assessment produces a scoped follow-up candidate or a recorded
  rejection.
