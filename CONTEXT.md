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
plant ticks — never by subtracting stamps directly. A decision may never
be *resolved* on an untranslated pair; where the platform still measures
one — a promotion's declared **claim basis**, this run's tick against the
tracked line's last stamp — the measurement bounds what the decision may
do and is refused by name once it leaves the recorded window, rather than
deciding anything on its own.

### Seq domains

`seq` is minted by three distinct counters on the served surface, and the
spelling alone never says which epoch a number belongs to — a consumer
cannot tell from it whether its cursor survives a restart. Contracts and
comments name the stream a seq belongs to with the terms below (decision
104); the epoch scope each carries is the invariant every cursor site holds.

**Publication seq**:
`Publication.seq` — served as the snapshot's `publication.published`
identity — and the routed emissions' `EventRecord.seq` beside it. Run-scoped:
each process lifetime's store bind opens a new epoch at seq 1, and nothing
persists the axis.
_Avoid_: "journal seq" for either; "seq" unqualified

**History-ring seq**:
`HistorySample.seq` on each point's volatile ring. Run-scoped: each
lifetime's ring begins empty and rides the serving run's tick domain, and
the retained samples never cross a restart. The `PointHistory.run` envelope
mark stamps the serving epoch on every answer, an emptied page included.
_Avoid_: "journal seq"; "seq" unqualified

**Journal seq** / **durable seq**:
`JournalEntry.seq` over a configured journal file and `DurableEntry.seq` over
a configured history file are the durable streams, file-scoped: startup
replay continues the file's numbering across restarts, `run_boundary` markers
segment its lifetimes, and a seq is never reused within a file. The file, not
the process, owns the epoch. A served journal tail with no journal file
configured is one more run-scoped axis — file scope comes from the configured
file, not the spelling.
_Avoid_: "run seq" for either

**Cursor reset-or-gap**:
A `since` cursor over a run-scoped domain held across a restart must
reset-or-gap, never silently starve: the new epoch's numbering can never
pass a dead cursor. The reset signals are already served — a regressed
publication identity, a newly served `run_boundary`, a changed `run` envelope
mark — and observing any one abandons the cursor for a whole re-read. A
file-scoped cursor needs no reset: the file's continued numbering is the
continuity guarantee.
