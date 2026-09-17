# Product requirements

Requirement files are the bridge between market evidence and implementation work. Each requirement has a stable ID, a statement of observable need, acceptance evidence, and a status.

Use these statuses:

- `candidate` — plausible need that still requires research or customer validation;
- `accepted` — enough evidence exists to plan implementation;
- `implemented` — the behavior exists and its verification is linked;
- `deferred` — deliberately outside the current product phase, with a revisit condition.

Implementation issues cite requirement IDs in their scope. Research findings distinguish source facts, DCS product decisions, and assumptions. Do not silently turn a vendor feature into a product requirement.

Open assumptions needing customer validation are consolidated per market pilot under `docs/research/`; the water/wastewater pilot baseline is `docs/research/pilot-acceptance.md`, which classifies each open item as satisfied-by-product, a first-client decision with a named owner, or out-of-scope with a revisit condition.
