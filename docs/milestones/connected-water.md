# Connected water station milestone

Assigned after PR #1526, based on `f53400ed`. This extends the existing station;
it does not create a second executor, alarm engine, UI application, or market.
Water/wastewater remains active and automatic backlog generation remains disabled.
The implementation is review work, not a published release or pilot acceptance.

## Inventory and actual gaps

| Family | Existing engineering/runtime contract and transitions | Diagnostics, alarms, persistence and redundancy | Operator surface and evidence | Gap closed in this slice |
| --- | --- | --- | --- | --- |
| Motor/pump | `PumpConfig`, `PumpInputs`, typed `pump`; composed `motor`, Boolean gates, interlocks, timer. False mode selects automatic allocation; true selects manual request after holdout. Service and protection inhibit both. Allocation availability is distinct from fault and run permission. | Run disagreement is timed and auto-clears on agreement. Thermal/moisture/feedback managed alarms. Acknowledgment does not reset protection. Existing block checkpoint and pair receipt adoption. | Model-owned pump, standard symbol, mode/request/service controls, declared summaries and member interfaces. `tests/pump.rs`, `tests/pump_station.rs`, customer station acceptance. | Reuse unchanged motor semantics. Applied pump outputs now transport water to the next vessel; reported feedback remains independent. |
| On/off valve | Existing Boolean motor command/feedback timing; new typed `on_off_valve` equipment composes service, permissive, interlock and gate. Opening is requested; safe command is closed. | Timed failure to achieve requested open contact, managed feedback alarm, checkpointed timer. Untrusted permissive inhibits opening. | Existing valve glyph with explicit opening/closing labels, service/request controls, feedback/alarm panel. Area scenario closes/reopens it and proves failed-contact alarm, acknowledgment and recovery. | Coherent reusable composition was missing. One open contact cannot prove independent closed position, isolation integrity or leakage. |
| Modulating actuator | Existing Float `valve`, `manual-station`, `interlock`. Typed `modulating_valve` composes false=automatic/true=manual, 5 percentage-point/scan transfer, protected output and position feedback. | 3% tolerance, 30-tick discrepancy diagnostic and managed alarm. Mode/request/transfer/diagnostic state use normal checkpoints. Bad measurement protects manual too. | Standard valve symbol, requested/applied/actual values in percent, mode/manual controls, interface tuning. Fault, recovery and both modes exercised. | Packaging and clear distinctions between demands and physical position; no second valve algorithm. |
| Analog measurement | Existing typed `AnalogInputSpec`, quality-preserving identity scaling in engineering units. `measurement` adds explicit warning and delivery. | Managed low/high warning, acknowledgment and reasoned bounded shelving. Warning is not a trip. Bad/stale PV protects its consumer. | Numeric value, unit, quality, explicit gauge and normal band; bounded history plot and member alarm controls. | Gauge bounds and equipment ownership were absent; display bounds remain separate from warning/control bounds. |
| Control loop | Existing `PidSpec`, held output on bad PV, persistent integral and previous measurement. `level_loop` supplies a draining PI demand; actuator owns operating mode. | Conditional integration at saturation. Negative-gain anti-windup corrected using the integral contribution's direction, with a regression test. | PV, setpoint and automatic demand, typed parameter editors and receipted numeric control. | Authoritative setpoint/manual limits and negative-gain saturation behavior. |
| Alarm | Existing managed Boolean/analog latching alarms, rationalization, acknowledgment, shelving/out-of-service/suppression. No new alarm engine. | Unacknowledged latch, bounded shelving, ordered attributed journal, checkpoint/receipt transfer. Acknowledgment does not clear a standing process condition. | Existing alarm strip, census, equipment alarm status, event/history surfaces and generic controls. Existing `reference-plant/ci/legs` alarm/pair/restart proofs. | Area warnings and actuator faults connected to those surfaces; request outcomes checked directly. |

Block interfaces are the existing typed ports, parameters, state, named commands
and events adapted into `BlockInterface`. Equipment references those same
components and points. Optional typed control roles identify mode explicitly;
the symbol does not guess from a label. Numeric control limits enter `PointMap`
and are enforced at admission and application for writes and forces. Applied
means the request was stored; protection and physical feedback determine the
resulting equipment output. The UI lists active interlock causes from declared
ports and bindings. The [contract review](connected-water-contract.md) records
shared-model changes and dependency decisions.

## Engineered area and source relationship

`crates/dcs-build/src/water_area.rs` declares one topology:

```
36 m3/h influent → TK-101 wet well → duty/standby pumps → TK-201 balance tank
                                                → XV-201 isolation → LV-201 → receiving water
                                                 LT-201 → LIC-201 → LV-201 automatic demand
```

The [Peel owner narrative](https://peelregion.ca/sites/default/files/2024-08/peel-wwps-standard-pcn-may-2022.pdf)
(version 1.1, May 2022, §§3.2 and 5.1–5.3) supplies duty/standby, operating-mode,
protection and failure prompts already interpreted in
[station policy research](../research/pumping-station.md).
[Festo's water training description](https://www.festo.com/de/en/e/technical-education/learning-solutions/process-automation-water-management/eds-water-management-id_3755840)
informs the connected transport/storage/control exercise. The balance vessel
and its discharge loop are original DCS training engineering, not a reproduction
of a vendor project or a claim that a utility uses these exact dimensions.
The cited sources do not supply these invented simulation parameters.

The code declares the following behavior:

| Quantity/policy | Declaration |
| --- | --- |
| Scan and field step | 0.2 seconds; simulation is driven by steps, never UI time |
| Inflow / per-pump delivery | 36 / 72 m3/h; applied pump output gates each negative wet-well draw under instantaneous motor response |
| Vessel cross-section / storage | 0.2 m² each; initial wet/balance levels 2.5 / 2.0 m; physical bounds 0–5 m |
| Balance inlet | Negative sum of wet-well pump draws, preserving transferred volume before clipping |
| Net storage derivative | `(inflow - outflow) / (3600 * tank_area)` m/s; bounded Euler integration |
| Outlet flow | `1.44 * physical_opening_percent` m3/h while isolation output is open; zero when closed |
| Modulator dynamics | Physical position lag τ=1 s; reported position lag τ=0.2 s |
| Wet-well sensors | Independent τ=0.2 / 0.4 s lags from physical storage; primary/backup failover remains existing station policy |
| Balance loop | PI kp=-60, ki=-0.4, kd=0, dt=0.2; 0–100% output; initial setpoint 2.0 m |
| Operator limits | Setpoint 0.8–3.8 m; manual opening 0–100%; typed authoritative inclusive bounds |
| Balance warning | Low 0.4 / high 4.2 m; 0.1 m hysteresis; 300-tick reasoned shelving ceiling |
| Flow warning | Low 0 / high 130 m3/h; notification only, standard measurement alarm lifecycle |
| Display | Level 0–5 m, balance normal 0.8–3.8 m; setpoint 0–5 m; discharge 0–144 m3/h, normal 0–100 m3/h |
| Feedback/freshness | Pump/isolation failure budget 15 scans; modulator 30 scans; new field measurements freshness budget 5 scans |

This is intentionally fast training storage, not hydraulic sizing. Pumps have
constant flow when running: head, pump curves, pipe pressure, leakage, cavitation
and water hammer are not modeled. Isolation moves instantly and has one reported
open contact. Tank clipping represents spill or unavailable negative storage;
there is no separately drawn, conserved overflow stream. Warnings precede the
upper boundary. Physical position and flow carriers are separate from reported feedback/flow
quality so a failed sensor does not freeze the hydraulics. Pump and isolation
mechanics respond instantaneously to the applied output; their reported contacts
are independent fault-injection points. Flow sensors have a 0.2-second lag. A sensor quality fault does not
rewrite physical storage. The old station dynamics fixture remains unchanged. These reproducible rigs
advance field time through driven control steps. Restart/takeover evidence
therefore proves state/output continuity in that clock domain; it does not
measure wall-time handover latency or physical process evolution during a
controller outage. Field output hold, outage limits and real-time commissioning
still need owner-specific evidence.
Each drawn process pipe corresponds to the above transfer path; control signal
relationships are not decorative fluid pipes.

## Run and engineering experience

From a platform development checkout:

```sh
python3 scripts/verify.py --phase rust-demo
python3 scripts/pump_demo.py run --area water --listen 0.0.0.0:9081 --plant-listen 127.0.0.1:9012 --no-browser
```

The generic controller and monitor run the emitted model. Select the balance
reading/setpoint, isolation or control valve to operate them. Closing XV-201
blocks discharge, the balance level rises and warns; reopening restores flow.
Manual 30% gives approximately 43.2 m3/h after the physical lag. The PI continues
computing in manual; the existing manual station limits transfer to 5 percentage
points per scan when returning to automatic. External integral tracking is not
introduced, so this is a rate-limited transfer rather than a claim of bumpless
PID integral tracking. Protection overrides that rate limit to apply its safe
output. Mode, requested
output, protected/applied output and actual feedback remain distinct. Stored
requests can remain standing behind protection; acknowledge does not reset them.
The discharge gauge opens the same reusable measurement panel as the balance level. The wet-well panel exposes the existing station's threshold, group and failover
interfaces plus its managed alarms. The UI retains the light navigation,
alarm strip, central SVG schematic and equipment pane. Numeric history uses the
same explicitly declared display bounds when available. Seven operational values declare a one-second durable recording cadence. No chart dependency
was introduced; existing SVG/history and serde contracts suffice for this scope.

The independent customer's [run guide](../../reference-plant/CONNECTED-WATER.md)
documents the required exact release repin and generic runtime commands. Its
`src/bin/connected-water.rs` calls the same supported
`water_area` API. `--add-pump` changes the engineered pump count to three and
obtains standard control, alarms, panel metadata and drawing without JavaScript.
The API currently supports two or three pumps in this particular area drawing;
the general `pump` composition remains separately usable.

## Reproducible evidence and remaining gates

`crates/dcs-build/tests/water_area.rs` compares every engineered point sample
at every scan and all terminal receipts across repeated runs. It also proves
protected manual operation, field fault/recovery, numeric write/force admission,
limits after checkpoint restoration, valid public
composition and another equipment instance. `dcs-sim` checks storage bounds,
gate quality/type validation and field checkpoint continuation. The PID regression
covers draining saturation in both directions. Model tests retain legacy loading
and structural/schema checks.

`reference-plant/ci/connected_water.py` uses the existing `simulate`, `consumers`,
`restart` and declared-pair rigs. `--extended` exercises all six consumer schedules,
durable controller restart and a converged pair switch on this exact area. It
covers normal operation, setpoint rejection/change, manual/automatic transfer,
blocked discharge, failed feedback, bad/stale measurement, warning, acknowledgment,
reasoned shelving, expiry and recovery. It checks every admitted request's terminal
receipt and compares every point value/quality, component state and complete
command/outcome/actor payload at each scenario boundary across consumer schedules
and restart. The adapter uses the existing between-legs hook without changing
the shared harness. Boot-specific receipt mint identity is excluded from that
behavior digest; existing restart/pair journal and receipt audits separately
check mint identity and once-only adoption. The pair checks identical images and adopted receipt logs around
and after the documented demote/promote handover. This is compatible-model
controller takeover between peers built from the same candidate artifact.
Mixed-artifact semantic upgrades and arbitrary online model mutation remain
unproven; model/checkpoint shape alone cannot establish control equivalence.

`python3 scripts/verify.py` includes the new clean consumer proof in `rust-proofs`.
For a focused rerun, `python3 scripts/verify.py --phase water-area-proof` runs
that same required proof under the shared build gate; it does not replace the
full gate or rerun unchanged historical-release proofs.
`scripts/connected_water_proof.py` creates an immutable, tagged candidate artifact
origin in runner scratch, independently checks out customer source, resolves all
DCS dependencies from that Git artifact with a lock, builds generic tools from
that exact revision, checks deterministic emission and another pump, and runs the
operator scenario twice. Evidence is written to
`target/water-area-evidence/customer.json`, including model and binary SHA-256
identities, customer lock/source identity, preserved candidate and customer Git
bundles, and the emitted model/dynamics/scenario documents with their hashes.
The recorded candidate origin is a temporary `file://` release stand-in. To
replay that exact locked checkout offline, restore the candidate bundle at the
recorded origin path and clone the customer bundle elsewhere. Alternatively,
repin the same candidate tag/commit at a new artifact origin and regenerate the
lock; that changes source-URL/checkout identity while retaining the artifact.
This is evidence preservation, not a public registry or reproducible binary-build claim.
The three-pump customer also runs the operator scenario. This proves the independent build boundary against a versioned
candidate. It does **not** prove a public release cut: the assigned customer
manifest retains its pending v0.10.0 tag pin (the lock still names the preceding source revision) and gates the new binary behind
`connected-water`. The supervisor must publish and repin before claiming the
published-artifact criterion. Bounded-control models use version 2 so older
runtimes refuse them instead of ignoring numeric limits; legacy models keep
version 1. The older reference checks preserve their pending v0.10.0 tooling substitution
using the immutable PR #1526 source baseline. That record is uncut and its
customer engineering lock predates it; this is a staging stand-in, not a
published-release identity proof. The new candidate proof uses one exact
revision for engineering and tooling.

Local verification on 2026-10-10 completed the full `python3 scripts/verify.py`
gate: formatting, 4,512 supervisor tests, clippy, workspace Rust tests and all
five clean-consumer/reference proofs passed. Total elapsed time was 3,067 s;
the historical proof phase took 2,067 s. The final evidence-preservation rerun
of `--phase water-area-proof` passed in 301 s. Three timeout/once-only regression
checks and 14 workflow/build-gate checks passed separately after their changes.
The workflow upload itself awaits execution in reviewed CI; local tests are
not a claim that remote checks or a PR have been published.

The preserved candidate is `v0.11.0-rc.1` at
`0307473a497d129b251d5f0587530a6c5e3eabfd`; its clean customer source is
`e38a91f713d1258e4327c68fc3fcac08a610b5a5`. The model SHA-256 is
`cff82cafbbdb75f5053ed2e978c03c73f7cb73472412ee1a1e0b527113d937d5`.
All six consumer schedules, restart at tick 644 and same-artifact takeover hold
the authoritative digest
`ae2876507a5da669fb4daae1d2446b7cbd887df96bb86d9415d150c35d3dd48c`;
all 16 admitted operator requests settle applied. Named rejection checks remain
part of the scenario. Restoring both bundles outside the platform checkout
preserved the clean customer commit/lock, resolved locked metadata without path
dependencies, and reproduced the model/dynamics using copied, hash-matched
artifact binaries. All 152 production source files match the candidate, including
the preview's page bytes. `target/water-area-evidence/restored-customer.json`
records that restoration; `verification.json` records the gate results.

An earlier historical test hung after a 256-scan batch overflowed the released
64-entry state sink and poisoned its handler mutex. The history harness now
uses 32-scan durability-attested batches and bounded HTTP; timeout outcomes are
unknown and never retried. Both deterministic passes and both required tamper
failures still hold. This fixes the harness, not runtime overload recovery.
The environment denied SIGTERM cleanup of that old test fixture, so it remains
an explicitly reported cleanup blocker; no permission escalation was attempted.

Actual desktop/phone, keyboard and reduced-motion browser review remains a
required gate. The available computer-use tool returned no browsers and opening
the in-app browser returned `Browser is not available: iab`. JavaScript syntax
and contract checks do not replace visual or interaction review. Keep this gate
open, including abnormal states and focus while editing during polling.
The network preview is available at `http://192.168.178.107:9081/` under the
user service `dcs-water-area-preview.service`; the existing port-9080 pump demo
continues separately. `systemctl --user status dcs-water-area-preview.service`
and `journalctl --user -u dcs-water-area-preview.service` inspect it.
`systemctl --user restart dcs-water-area-preview.service` creates a fresh training
run; `stop` ends it. Each run's generated model/dynamics, checkpoint, journal and
history reside in `target/pump-demo/<timestamp>/`. The service is a transient
simulated preview, not an installed production deployment. HTTP and source-state
checks are recorded separately from the still-blocked actual browser gate.

## Reviewable delivery sequence

The supervisor owns staging, commits, PRs, CI and release publication. Suggested
independently gated cuts of the assigned edits are:

1. Shared contract/validation/authoritative numeric limits, simulation declarations
   and negative-gain PID regression; adapt existing struct constructors.
2. Reusable equipment compositions, connected area and deterministic Rust tests.
3. Derived operator presentation, gauge/trend context, protection explanations
   and the existing launcher's water-area option.
4. Independent candidate-artifact consumer, existing-harness operator/restart/pair
   proof, required proof registration and evidence/docs reconciliation.

Run the repository gate on each concrete cut. This working tree is not a claim
that four PRs have been published or reviewed. No automatic backlog generation,
issue reservation or unrelated Git work is performed.

Open customer policies remain: protection latching versus automatic recovery;
manual restart holdouts and standing-demand resumption; closed-position proof;
alarm priority vocabulary, warning limits and shelving roles; tuning permissions;
fail-safe position and physical process accuracy. Simulation evidence does not
answer site hazard analysis, identity/authorization or commissioning.
See the [activation proposal](connected-water-activation.md) for lifecycle gaps.
