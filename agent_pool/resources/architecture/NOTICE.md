# Architecture review resources

This directory supplies the pinned reference material for the daily
architecture reviewer. It is installed with the pinned supervisor release;
the supervisor verifies every file's SHA-256 against `MANIFEST.json` before
launching a review run.

## Project-owned files

- `policy.md` — the DCS autonomous role policy. It takes precedence over the
  reference workflows below: no candidate-selection questions, grilling,
  manual approvals, or waiting for a user.
- `prompt.md` — the reviewer prompt template, formatted by
  `agent_pool/review.py` at launch time.

## Vendored references (`references/`)

Source: the `skills/engineering/` tree of <https://github.com/mattpocock/skills>,
MIT licensed (see `LICENSE`). Vendored from the local installations inspected at
`C:/Users/Kaspar/.codex/skills/` on 2026-09-14; upstream revision of record is
`321658273cb1` (2026-08-19). Local copies contain tool-specific front matter and
agent metadata not present upstream; content is vendored unmodified from the
local copies, which are the provenance of record. Local adaptations, if any are
ever needed, must be recorded in `MANIFEST.json` under `adaptations` rather than
edited silently.

- `codebase-design/` — deep-module vocabulary: module, interface, depth, seam,
  adapter, leverage, locality; the deletion test; dependency categories and
  seam discipline. `DESIGN-IT-TWICE.md` describes an interactive parallel
  sub-agent workflow; the autonomous reviewer does not invoke it.
- `domain-modeling/` — glossary (`CONTEXT.md`) and ADR formats. The
  conversational questioning and inline-editing steps are superseded by
  `policy.md`: the reviewer records proposed glossary/decision edits in its
  report and workers apply accepted edits in implementation work.
- `improve-codebase-architecture/` — exploration and candidate-evaluation
  guidance. Its interactive selection and grilling workflow is not invoked;
  `HTML-REPORT.md` is retained for reference only — the lane emits the
  versioned JSON report plus a readable Markdown report, never an interactive
  HTML workflow.

Updates to vendored files require a reviewed change and a pinned supervisor
upgrade; the lane never downloads references at run time.
