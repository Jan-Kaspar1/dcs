# `dcs-ethercat` tests

Two test shapes live here. `contract.rs` drives the cyclic contract from
hand-written scripts through `testing::FakeTransport` — the contract we
*intended*, expressed directly. `replay-recorded.rs` drives it from the
recorded link runs in `captures/`, through `replay::ReplayTransport` —
the contract we *got*, expressed as a device actually behaved.

Both run on any host with no NIC, no rig, and no privileges. Physical
acceptance belongs to the Lenovo QA lane
(`docs/lenovo-hardware-qa-plan.md`); nothing here establishes hardware
acceptance or host deployment.

## Recorded-link replay

A **capture** is a JSON document recording one bus run: what discovery
found, and what each exchange answered, in order.

```
captures/
  wago-750-354-known-good.json   # complete × 4, late, station-0 short, recovery
  wago-750-354-link-flap.json    # complete × 3, dropped × 3, complete × 2
```

Each cycle is `{"outcome": …, "inputs": "<hex>"}` where `outcome` is one
of the transport seam's spellings — `complete`, `late`, `short` (with an
optional `station`), `failed`, or `timeout` — and `inputs` is the returned
input image as hex. Past the end of the recording the transport repeats
the capture's **last** cycle, so a capture of `n` cycles can drive an
unbounded run. A recording that ends mid-outage therefore keeps failing:
the replay cannot recover into a `Complete` the capture never recorded.

A malformed capture is refused by name — an odd hex length, a non-hex
byte, an image longer than the bus's input area, an unrecorded outcome, a
station record without its identity fields — rather than replaying as
something other than what it records.

### What a capture proves, and what it does not

A capture is a recorded *answer* sequence, not a decoded frame stream. It
replays the transport and driver layers: the `CycleOutcome` verdicts, the
staged output image's publication, the latched input image and its
acquisition stamp, held-image aging, `exchange_miss_threshold` escalation,
working-counter attribution, and recovery re-entry at the exchange
boundary.

It does not prove the EtherCrab socket path, frame encoding, or timing.
Those stay on real hardware.

### Recording a new capture

1. Capture the run on the rig (the QA lane owns the physical session) and
   reduce it to the per-exchange answers: the outcome each cycle reported
   and the input image it returned.
2. Write the document under `captures/` with a `name` matching the file
   stem, a `description` recording what the run was and what makes this
   case worth keeping, and the cycles in order.
3. Add the case to `CAPTURES` in `replay-recorded.rs` and assert the
   per-outcome counters, held-image aging, threshold escalation, and
   recovery the recording exhibits.
4. If the case came from a live-rig incident, say so in the description.
   That is the point of the pattern: each incident becomes a permanent
   regression test instead of a one-off QA finding.

The runtime's own bus fixtures — `wago_rig.json` and
`wago_rig_cyclic.json` under `crates/dcs-demo/fixtures/` — are emitted
artifacts and byte-pinned by the build tests; the recorded coupler
identity in a capture matches the rig manifest's station 0 rather than
editing them.