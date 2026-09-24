# DCS Platform

The process-control platform's shared domain language: canonical names for
concepts whose everyday words collide across crates, contracts, and docs.

## Language

### Tick domains

A `Tick` is minted by three distinct counters, and the bare word "tick" has
historically named all of them. Contracts and comments name the domain a
tick belongs to with the terms below; the comparability rule under them is
the invariant every stamp and comparison site holds.

**Run tick**:
The executor's own scan counter — one per scan — and the run's attribution
domain: journal entries, history samples, command receipts, and role
reports are all stamped in it.
_Avoid_: "the tick" unqualified, wall-clock readings of it

**Plant tick**:
The simulated field's step counter — what `SimDriver::tick`/`step` report,
`PlantResponse::Stepped.tick` carries, and the sim checkpoint's `tick`
field persists. A paused or stopped plant's counter freezes while the
reading run's ticks on, so the two domains drift.
_Avoid_: "the tick" unqualified, "step" for the counter itself

**Source tick**:
The tracked checkpoint stream's counter — the producing run's run ticks as
a tracking peer receives them. A tracking apply translates each source tick
into the local run domain at `tick + tick_offset`; after a source restart
the two counters differ by the generation offset the resync left.
_Avoid_: "the tick" unqualified, "checkpoint tick"

**Same-domain comparability**:
Two ticks order and subtract meaningfully only when minted in the same
domain; a cross-domain comparison is meaningful only after explicit
translation — the apply offset for source ticks, the freshness record for
plant ticks — never by subtracting stamps directly.
