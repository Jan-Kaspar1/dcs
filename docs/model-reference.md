# Plant-model document reference

The plant model is the single contract shared by engineering data, the
controllers, and the monitoring UI: one versioned JSON document declaring
the devices, logical I/O points, signals, component instances, and
connections of one controller's view of the plant. This file is the
document-side companion to `docs/integration-guide.md` — the guide covers
how component kinds and device kinds are implemented and registered; this
reference covers what a plant document declares and which layer checks it.

Where each contract lives in code:

- Document schema: `dcs-model` (`crates/dcs-model/src/model.rs`).
  `PlantModel::load` runs parse → version check → `PlantModel::validate`.
- Structural validation: `crates/dcs-model/src/validate.rs`
  (`ValidationError`). Advisory engineering-quality lint:
  `crates/dcs-model/src/lint.rs` (`LintFinding`/`LintRule`).
- Assembly-time checks — kind resolution, device-parameter contracts,
  port binding, requirement verification:
  `crates/dcs-assembly` (`AssemblyError`), with the device-kind
  factories in `crates/dcs-assembly/src/drivers.rs` and the `sim-bus`
  parameter grammar in `crates/dcs-sim-bus/src/params.rs`.
- The dynamics document: `dcs-sim`'s `ProcessElement` vocabulary
  (`crates/dcs-sim/src/map.rs`), consumed by `dcs-plant-server
  --dynamics` and by test rigs building a `ChannelMap` directly.

Every field name, kind string, parameter key, and element tag named below
is the exact identifier those sources read or write.

## Shared vocabulary

- **Ids** are unsigned 64-bit integers serialized as bare JSON numbers.
  `devices[].id`, `io_points[].id`, `signals[].id`, and
  `components[].id` are independent numbering spaces; an id must be
  unique only within its own collection.
- **`direction`** is `"in"` or `"out"`. For channels and io_points, `in`
  carries a value from the field into the controller and `out` carries
  one from the controller to the field. For ports, `in` consumes the
  value a connection delivers and `out` produces the value a connection
  carries away.
- **`value_type`** is `"bool"`, `"int"`, or `"float"` — the `snake_case`
  wire spellings of the `ValueKind` variant names from `dcs-core`. A
  document carrying the legacy PascalCase spellings (`"Bool"`, `"Int"`,
  `"Float"`) still loads — serde accepts them as aliases — but the
  canonical emitted spelling is `snake_case`.
- **A typed `Value`** — an io_point's `initial`, a component's
  `parameters` entry, a `sim-bus` register's `initial` — serializes as
  an externally tagged object: `{"bool": true}`, `{"int": -42}`, or
  `{"float": 2.5}` (with the same PascalCase read aliases on the tag:
  `{"Bool": true}` still loads). (`sim-scripted` script entries are the
  one exception: their `value` is a plain JSON scalar — see that kind's
  section.)
- **Optional fields** extend the version-1 schema without a version bump
  (decision 3): a document predating a field loads with the field unset,
  and an unset field serializes back without the key. `hardware` and
  `parameters` on a device, `channel`/`initial`/`writable`/`journaled` on
  an io_point, and `unit`/`description`/`group` on a signal all follow
  this convention.
- The parser ignores keys it does not know, so tool-added annotation
  keys load harmlessly; they are not part of the contract and the
  canonical model document — what `PlantModel::fingerprint` hashes —
  reserializes without them.

## Document shape and the load pipeline

A document is one JSON object with six required top-level keys, each a
section described below:

```json
{
  "version": 1,
  "devices": [],
  "io_points": [],
  "signals": [],
  "components": [],
  "connections": []
}
```

All six keys are required; every section may be an empty list. A checked-in
minimal example is `crates/dcs-model/fixtures/minimal.json`.

`PlantModel::load` answers three failures, in this order:

1. `LoadError::Malformed` — the text is not well-formed JSON or does not
   match the schema (a missing `version`, a mistyped field).
2. `LoadError::UnsupportedVersion` — `version` is not `MODEL_VERSION`
   (currently `1`); the error carries `found` and `supported`.
3. `LoadError::Invalid` — the document parsed but failed validation; the
   error lists every `ValidationError` found rather than stopping at the
   first.

Assembly (`dcs-assembly::resolve_drivers` plus `assemble`) then resolves
the validated model against the registered device and component kinds,
and `PlantModel::lint` reports advisory findings over it. Which check
lives in which layer is summarized at the end of this document.

## `version`

Required `u32`. `PlantModel::load` accepts only `MODEL_VERSION`
(`dcs_model::MODEL_VERSION`, currently `1`); anything else is
`LoadError::UnsupportedVersion`. Additive optional fields ride the same
version; a breaking schema change bumps it and adds a load-time
migration.

## `devices`

A list of field or simulated I/O devices. A device is the unit the model
maps logical I/O onto; the driver layer builds one backend per device at
assembly.

| Field | Type | Rule |
|---|---|---|
| `id` | `DeviceId` (u64) | Required; unique within `devices` — `ValidationError::DuplicateId { collection: "device" }`. |
| `kind` | string | Required; opaque to the model. Resolved by `dcs_assembly::DriverRegistry` at assembly — exact registration first, then prefix in registration order; an unregistered kind is `AssemblyError::UnknownDeviceKind`. The built-in kinds are in the next section. |
| `channels` | object: name → `{"direction": "in"\|"out", "value_type": "bool"\|"int"\|"float"}` | Required; may be empty. A channel is a physical endpoint an io_point binds to. A channel no io_point binds is lint `unbound_channel`, not an error. |
| `hardware` | bool | Optional; unset means `false`. `true` marks the device *hardware-bound*: its kind requires physical field hardware, and no simulated backend may serve it. The kind's factory enforces the marker both ways at assembly — a hardware-bound kind rejects a device omitting it (`InvalidDeviceParameters`), and a simulated kind rejects a device carrying it — so a model declaring a hardware kind fails startup if the hardware cannot initialize rather than silently falling back to simulation. |
| `parameters` | object: name → arbitrary JSON | Optional. Kind-specific addressing and configuration, opaque to the model — the registered device-kind factory owns all validation at assembly (`AssemblyError::InvalidDeviceParameters`). Unlike component parameters these are general JSON values, so a kind can carry strings and structured addressing data. |

Example:

```json
{
  "id": 1,
  "kind": "sim-8ai",
  "channels": {
    "ch0": { "direction": "in", "value_type": "float" }
  }
}
```

## `io_points`

A list of logical I/O points — the unit control logic binds to. A point
is either *field-bound* (carries `channel`) or *internal* (channel-less,
carried by the controller's scan image — decision 14).

| Field | Type | Rule |
|---|---|---|
| `id` | `PointId` (u64) | Required; unique within `io_points` — `DuplicateId { collection: "io_point" }`. |
| `direction` | `"in"` \| `"out"` | Required. `in` points are read into the scan image each scan; `out` points are written from the image to the field (or recorded, for internal points). |
| `value_type` | `"bool"` \| `"int"` \| `"float"` | Required; drivers reject mismatched writes. |
| `channel` | `{"device": <device id>, "name": "<channel>"}` | Optional. Present → a field point: the device must be declared (`ValidationError::UnknownDevice`), the channel must exist on it (`UnknownChannel`), and the point's `direction` and `value_type` must agree with the channel's (`ChannelDirectionMismatch`, `ChannelTypeMismatch`). Absent → an internal point. |
| `initial` | tagged `Value`, e.g. `{"float": 25.0}` | Optional; required when `channel` is absent (`MissingInitial`), and its variant must equal `value_type` (`InitialKindMismatch`). Forbidden when `channel` is present (`FieldInitial`) — the field owns a bound point's value. |
| `writable` | bool | Optional; unset means not writable. Valid on `in` points only — `writable` on an `out` point is `ValidationError::WritableOut`. |
| `stale_after_ticks` | u64 | Optional; unset means no freshness check. Valid on field-bound `in` points only — on an `out` point it is `ValidationError::StaleOut`, on a channel-less internal point `StaleInternal`. |
| `journaled` | bool | Optional; unset means the point's value transitions stay out of the durable journal. Valid on `bool`/`int` points of either direction — `journaled` on a `float` point is `ValidationError::JournaledFloat`. |

### Internal points

An io_point declared without `channel` is image-carried rather than
driver-served (`IoPoint::is_internal`). The scan image seeds it at
`initial`:

- an internal `in` point holds `initial` until a command writes it
  through the receipt-answered command path — the ordinary operator-value
  mechanism (setpoints, mode switches) — or an internal link routes onto
  it;
- an internal `out` point records the value a component writes to it,
  for monitoring and for internal-link routing;
- a declared internal `in`/`out` pair wired point-to-point carries a
  connection inside the image (see `connections`).

Internal points reach no device factory and no driver — they are the
controller's own state, appear in telemetry like field points, and ride
checkpoints (internal `in` samples in the checkpoint's `internal`
section, `out` samples in `outputs`), so a commanded setpoint survives a
switchover rather than reverting to `initial`.

### `writable` and the command surface

`writable` marks the io_points an operator may write — the model-declared
command surface (decision 18). A `write_value` command on an unmarked
point, or on any `out` point, is refused at submission with
`CommandError::NotWritable`; `force_point`/`unforce_point` (the
persistent input-forcing pair, decision 21) accept writable `in` points
only. Per point kind:

- a writable *field* `in` point's command write is forwarded to the
  driver at the scan boundary and the same scan's input phase reads it
  back — documented operator substitution of the input image, holding
  until the field side asserts a different value;
- a writable *internal* `in` point takes the write in the image and
  holds it until the next command — the common setpoint target.

A channel-bound `writable` point is part of the operator surface worth
reviewing, so lint flags it (`writable_field_point`); writable internal
points are the ordinary mechanism and are not flagged. Writability joins
the point metadata the monitoring surface serves, so the page offers
command affordances only where commands can succeed.

### `stale_after_ticks` and input freshness

`stale_after_ticks` declares how fresh a field `in` point's samples must
stay: the number of executor scan ticks a driver-stamped sample tick may
lag before the point's data is stale (decision 45). The budget lives in
the point map assembly produces, and the *executor's input phase*
applies the rule — each scan, for a budgeted field `in` point, it
compares the tick the driver returned on the sample against the scan
tick before re-stamping:

- a lag within the budget leaves the driver-returned quality untouched;
- a lag exceeding it merges `Uncertain(Stale)` by the worst-of rule, so
  a driver-reported `Bad` or worse-named `Uncertain` is never improved,
  while a held `Good` value degrades to `Uncertain(Stale)` until the
  first sample inside the budget returns it to `Good`;
- the landed image sample always carries the scan tick — the executor
  is the only timestamp authority; the driver tick is freshness
  evidence, never an image timestamp;
- a failed read is not a stale sample: the documented `Bad` mapping and
  last-known-value behavior stand, and a forced point never reads the
  driver, so `Substituted` stands too.

A budget of `0` requires a sample stamped at the current scan tick —
the strictest declaration, for sources expected to refresh every scan.
The sim bank, the remote plant, and the sim-bus register bank all stamp
their writes with a device tick the driver protocols carry, so field
devices integrated through them supply freshness evidence without
protocol changes. A driver whose samples carry no usable freshness
signal — one that stamps every read with the current tick, or a fixed
tick — simply makes the declaration inert or always-stale; declare the
field only where the source distinguishes fresh samples from held ones.

### `journaled` and the durable transition record

`journaled` marks the io_points whose observed *value* transitions join
the durable transition journal — the low-volume, file-backed record of
settled command receipts and quality transitions (decisions 36 and 74).
Every point's samples already land in the bounded per-point history
ring; that ring is volatile telemetry, and bounded eviction drops old
samples. The journal is the audit-grade record: marking a point
`journaled` makes each of its value changes durable as a
`point_changed` journal event carrying `point`, `from`, and `to` —
`from` reading `null` on the point's first observed sample, the
`quality_changed` convention — attributed to the producing scan's tick
and ordered after that scan's quality transitions in ascending point
order.

The declaration is deliberately opt-in and bounded:

- either direction may be journaled — the alarm-lifecycle status points
  the decision names (`alarm`, `unacknowledged`, `shelved`,
  `suppressed`, `out_of_service`) are component `out` status points, and
  mode or protection-layer states ride the same mechanism;
- `float` points cannot be journaled (`JournaledFloat`): a per-scan
  measurement stream would churn the durable record, and a float
  transition belongs in the history ring, not the journal;
- an undeclared point's value changes produce no journal entries — the
  journal stays the low-volume record decision 36 describes, instead of
  duplicating every point's history stream.

A receipted write to a journaled writable point produces *both* its
attributed `command_settled` entry and the resulting `point_changed`
entry, in `seq` order — the action's attribution and the transition it
caused are each auditable. A component-driven transition on a journaled
status point lands with no receipt, and the durable journal file
replays `point_changed` entries like every other event, continuing
`seq` numbering across a restart.

## `signals`

A list of plant signals: the monitoring/UI-facing names for the values
io_points carry. Signals are metadata — they change nothing the
controller computes.

| Field | Type | Rule |
|---|---|---|
| `id` | `SignalId` (u64) | Required; unique within `signals` — `DuplicateId { collection: "signal" }`. |
| `name` | string | Required; the human-facing signal name. |
| `source` | `PointId` | Required; must name a declared io_point (`ValidationError::UnknownSource`). Several signals may source one point; `SignalIndex` resolves the lowest signal id. |
| `unit` | string | Optional; engineering unit of the carried value, e.g. `"degC"`. Display metadata — no wiring rule; absence is lint `signal_missing_unit`. |
| `description` | string | Optional; human-facing description. Absence is lint `signal_missing_description`. |
| `group` | string | Optional; display group the monitoring page files the signal under — the plant area or unit it belongs to (decision 23). Pure display metadata: any string is a valid group and signals sharing a group name are simply listed together; ungrouped signals render under the documented `"ungrouped"` default. Absence is lint `signal_missing_group`. |

`PlantModel::signal_index` derives the `SignalIndex` the monitoring
surface serves: one `PointSignal` per declared io_point, ordered by
`PointId`, joining the signal's `name`/`unit`/`description`/`group` with
the point's own `direction`/`value_type`/`writable`. A point no signal
sources still appears — `name` falls back to `"point-<id>"` and the
metadata fields are null — and lint flags it `point_without_signal`.

## `components`

A list of component instances: instantiations of reusable component
kinds. Components step in `components` order — the model's declared scan
order.

| Field | Type | Rule |
|---|---|---|
| `id` | `ComponentId` (u64) | Required; unique within `components` — `DuplicateId { collection: "component" }`. |
| `kind` | string | Required; opaque to the model — the name a `dcs_assembly::ComponentRegistry` maps to a constructor at assembly (`AssemblyError::UnknownComponentKind`). The shipped controller registers the `dcs-blocks` kinds (`analog-input`, `pid`, `latching-alarm`, `pump-group`, `timer`, …); `dcs-controller`'s `registry()` is the list it deploys. |
| `parameters` | object: name → tagged `Value` | Required; may be empty. Kind-specific construction data checked by the kind's `from_parameters` at assembly — a missing or invalid entry is `AssemblyError::Component` wrapping `ParameterError`. A kind's parameter names, value kinds, and ranges are published by its `describe()` `ComponentDescriptor` (served per instance in `TelemetrySnapshot.descriptors`) and mirrored as data by `dcs-build`'s specs. |
| `ports` | object: name → `{"direction": "in"\|"out", "value_type": "bool"\|"int"\|"float"}` | Required; may be empty. The signature the model wires; `connections` bind ports to points or other ports. Assembly requires every declared port bound exactly once (`UnboundPort`, `PortBoundTwice`), and the constructed component's declared `IoRequirement`s are checked against the resolved point map — `UnmappedPoint`, `DirectionMismatch`, `TypeMismatch`. |
|| `rationalization` | object: `consequence`/`required_action`/`reference` strings | Optional; absent serializes to nothing. The decision-70 prose half of an alarm instance's rationalization record — the consequence of inaction, the required operator action, and the display/procedure reference. The model stores it uninterpreted; the alarm kinds' construction requires it (see below). |

The model does not validate `parameters` contents or that a `kind`
exists: kind resolution and parameter checking are assembly's, because
the registry is a deployment choice. `docs/integration-guide.md` covers
declaring a kind's parameters; per-kind contracts live beside each
`dcs-blocks` kind's `KIND`/`from_parameters`/`describe`.

Some kinds are variable-arity: the declared `ports` set fixes the
instance's size at assembly. `interlock` declares `trip_1` … `trip_N`;
`pump-group` — the duty/standby group the station decisions record —
declares the four-member family `cmd_i`, `run_i`, `fault_i`, `avail_i`
per managed pump `i`. The highest bound index sets the count and every
index below it must bind the whole family — a gap or partial family is
`AssemblyError::UnboundPort` naming the missing member. The group's
rotation policy, staging, and status-output contract live beside
`PumpGroup::KIND`.

The station level-control contract architecture decision 42 records
adds two fixed-arity kinds. `threshold-chain` reads `level` (`in`,
`Float`) and drives `demand` (`out`, `Int`) — the stage count a
`pump-group`'s `demand` consumes — plus the `Bool` flags `duty_call`,
`lag_call`, `below_cutoff`, and `high_level`. Its `parameters` are the
ordered setpoint table `cutoff` < `stop` < `start` < `lag_start` <
`high` (all finite `Float`s, all operator-tunable through
`SetParameter`, a retune breaking the ordering refused naming the
parameter) and `on_bad_demand` (`Int`, `0`–`2`): the stage count the
chain emits while `level` is non-`Good`, the decision's declared
answer to a failed measurement. `failover-select` is parameterless:
`primary` and `backup` (`in`, `Float`), `out` (`out`, `Float`)
carrying the selected sample with its quality, and `backup_active`
(`out`, `Bool`) asserted while the backup serves. The switch rule is
quality-driven — `out` carries `primary` while it reads `Good` with a
finite value, else `backup` verbatim, quality included — and the
return rule is immediate: a primary reading `Good` again re-selects
the same scan, no latch. The pair wires
`failover-select.out` onto `threshold-chain.level` through a linked
point pair — an internal link carries quality, a field loopback does
not — and `threshold-chain.demand` onto `pump-group.demand` likewise.
`backup_active` takes the same route into a `bool-latching-alarm`'s
`in` — the alarmed backup-mode engagement decision 43 maps — with a
model-writable `ack` point releasing the latch.
`crates/dcs-assembly/fixtures/station_level.json` is the recorded
composition, manual-takeover gates included; per-port semantics live
beside `ThresholdChain::KIND` and `FailoverSelect::KIND`.

The Bool-input latching sibling architecture decision 43 records adds
one fixed-arity kind. `bool-latching-alarm` keeps `latching-alarm`'s
`in`/`ack`/`alarm`/`unacknowledged` vocabulary exactly, with `in` a
`Bool`: `alarm` follows the input directly — no hysteresis and no
standing-limit parameter — and
`unacknowledged` latches the input's false-to-true edge, clearing while
the model-wired writable `ack` point reads `true` under the same
level-sensitive, ack-dominates rule (a held `ack` suppresses a fresh
latch). Both outputs carry the worst of the two inputs' qualities.
`crates/dcs-assembly/fixtures/bool_latching_alarm.json` is the recorded
composition — a `motor`'s `fault` output carried through a declared
internal point pair into `in`; per-port semantics live beside
`BoolLatchingAlarm::KIND`.

The alarm rationalization record architecture decision 70 records
makes the model the master alarm database. An alarm's identity is the
component instance plus the `Signal` on its standing `alarm` point —
no separate alarm tag — and its rationalization data lands in two
halves. The numeric half is three declared parameters every alarm
kind carries — `latching-alarm`, `bool-latching-alarm`, and the
managed siblings below: `priority`, `class`, and `response_ticks`,
all required non-negative `Int`s served live through the snapshot's
parameter section, tunable through `SetParameter`, and checkpointed
like any declared parameter. The site vocabulary the codes name —
priority levels, class rules — is an open customer assumption the
model carries as data, not meaning. The prose half is the optional
`rationalization` block on `ComponentInstance`: `consequence` (of
inaction), `required_action`, and `reference` (the display/procedure
the operator consults) — stored uninterpreted and absent serializing
to nothing, so documents predating it load unchanged. Enforcement
lands where kind and instance meet — construction of a declared
alarm kind rejects an instance missing any of the three parameters
or carrying no complete non-empty prose record, the failure naming
the element through `AssemblyError::Component` and surfacing through
`dcs-controller --check`. The emitted JSON Schema pins the same
kind-conditional obligation where expressible.

The managed alarm lifecycle architecture decisions 71–73 record adds
two fixed-arity kinds, one managed sibling per latching kind.
`managed-latching-alarm` keeps `latching-alarm`'s
`in`/`ack`/`alarm`/`unacknowledged` vocabulary and its
`low_limit`/`high_limit`/`hysteresis` parameters exactly;
`managed-bool-latching-alarm` keeps `bool-latching-alarm`'s — `in` a
`Bool`, `alarm` following the input directly. Each adds the uniform
managed-state outputs `shelved`, `suppressed`, and `out_of_service`
(`out`, `Bool`, `Status` role), three optional `in`/`Bool` inputs the
instance declares only where the model wires one, and the `Int`
`max_shelve_ticks` bound; both carry the decision-70
`priority`/`class`/`response_ticks` parameters like every alarm kind.
`shelve` and `oos` bind writable internal `In` points so operator
commands ride the receipted `WriteValue` path: a `true` level requests
the state, `false` returns it manually — out-of-service has no
automatic return. Shelving asserts `shelved` while the request stands
inside the bound, counts the request's asserting scan as the first,
and drops at `max_shelve_ticks` even while the request still stands —
a re-shelve requires the request to cycle through `false`. A zero
`max_shelve_ticks` declares never-shelvable; an unbound or unwritable
`shelve` point rejects stronger still, `NotWritable` at submission.
`suppress` binds declared wiring — designed or state-based — asserting
`suppressed` while it withholds `unacknowledged`'s latch; release
evaluates fresh, so a condition that outlasted its suppression arrives
as a new transition. While any managed flag stands, `alarm` keeps
reporting process truth and `unacknowledged` its latch — the flag
reroutes presentation, never erases the record: shelving an active
alarm leaves both flags standing, an ack mid-shelve clears the latch
normally, a trip mid-OOS evaluates and latches, and an unbound
managed input reports its flag standing-clear. The shelve-expiry
countdown, the out-of-service state, and the suppression state all
ride `capture_state`, so a tracking standby promoted mid-shelve
continues the remaining bound identically.
`crates/dcs-assembly/fixtures/managed_alarms.json` is the recorded
composition — three instances covering the full surface, a
bound-but-unwritable `shelve`, and an unbound `suppress`; per-port
semantics live beside `ManagedLatchingAlarm::KIND` and
`ManagedBoolLatchingAlarm::KIND`.

The chemical-dosing demand contract architecture decision 50 records
adds one variable-signature kind. `flow-paced-ratio` reads `flow`
(`in`, `Float`) — the measured process flow — `dose` (`in`, `Float`)
— the operator's dose setpoint per flow unit, wired to a writable
internal `in` point so writes ride the journaled receipted command
path and the held value crosses checkpoints — and `trim` (`in`,
`Float`) — the optional analyzer correction, declared on the instance
only where the model wires one, an unwired `trim` meaning unity. It
drives `demand` (`out`, `Float`), `clamped` (`out`, `Bool`), and
`fallback_active` (`out`, `Bool`) — the engagement flag the alarm set
consumes. Each trusted-`flow` scan emits
`clamp(dose, min_dose, max_dose) × flow × trim` clamped to
`[min_rate, max_rate]`, `clamped` asserting while either bound
engages. The `parameters` are the finite `Float` bounds `min_dose` ≤
`max_dose` and `min_rate` ≤ `max_rate` plus `fallback_rate`, the `Int`
code `on_bad_flow` (`0` stop — `demand` falls to zero; `1` hold the
last `Good`-stamped demand; `2` drive `fallback_rate`), and the `Int`
code `on_bad_trim` (`0` pace untrimmed on flow alone; `1` hold the
last `Good` trim); all seven are `SetParameter`-tunable. A non-`Good`
`dose` holds the last `Good` finite setpoint — `demand` zero and
`fallback_active` asserted until the first one arrives — and `demand`
always carries the merged worst-of input qualities, so a bad flow
marks the demand untrusted even under a hold or fallback response.
Fixed-rate service is not a kind mode: the operator's fixed demand
rides a `manual-station`, while `on_bad_flow` `2` makes `fallback_rate`
the declared fixed answer to a failed pacing signal.
`crates/dcs-assembly/fixtures/flow_paced_ratio.json` is the recorded
composition — three instances covering each `on_bad_flow` response,
one trim-bound; per-port semantics live beside `FlowPacedRatio::KIND`.

The dose-confirmation kind architecture decision 53 records adds one
fixed-arity kind. `deviation-monitor` reads `expected` (`in`, `Float`)
— the commanded chemical rate or total — and `measured` (`in`,
`Float`) — the measured consumption — and drives `deviation` (`out`,
`Float`), the last completed window's relative deviation, and
`deviating` (`out`, `Bool`), the dose-not-confirmed verdict the
decision-55 alarm set consumes. The `parameters` are
`deviation_limit` (non-negative finite `Float`, the tolerated relative
deviation) and `window_ticks` (`Int`, `1`–`i64::MAX`, the scans each
window accumulates); both are `SetParameter`-tunable. Each scan where
both inputs read `Good` banks the pair into the running window; a
non-`Good` reading on either input freezes the window — no bank, no
verdict change — while both outputs hold their last completed values
under the merged worst-of input qualities. At `window_ticks` banked
pairs the window closes: `deviation` updates to the accumulated
relative error, `deviating` asserts while `|deviation|` exceeds
`deviation_limit`, and the accumulators reset for the next window —
so a slow drawdown trend is judged on its windowed total rather than
instantaneously, and recovery clears the verdict at the next closed
window. The running sums, position, and standing verdict are run
state under decision 20's checkpoint rule. Where no measured-
consumption signal exists the model simply does not instantiate the
kind — commanded `totalizer` integration alone claims no
dose-confirmed verdict.
`crates/dcs-assembly/fixtures/deviation_monitor.json` is the recorded
composition — a windowed instance beside a window-1 instantaneous
instance over one scripted expected/measured pair; per-port semantics
live beside `DeviationMonitor::KIND`.

The filter-backwash shared-supply contract architecture decision 56
records adds one variable-arity kind. `backwash-coordinator`
arbitrates an exclusive grant across `N` filters declared
`request_i`/`grant_i`/`position_i` under the same indexed-family
convention `interlock`'s `trip_N` uses — the filter count is the
highest bound index and every index below it must bind all three. Per
filter `i`: `request_i` (`in`, `Bool`) is the armed backwash request,
`grant_i` (`out`, `Bool`) the exclusive supply grant — at most one
asserted at a time — and `position_i` (`out`, `Int`) the 1-based queue
position, `0` while the filter is not queued (the grant holder
included — it holds the grant, not a queue slot; `active` identifies
it). The bank-level `active` (`out`, `Int`) names the grant holder,
`queued` (`out`, `Int`) counts pending requests, and
`resource_blocked` (`out`, `Bool`) asserts while a request stands
first in queue and a grant permissive fails. The permissives are the
declared inputs `supply_ok`, `waste_ok`, `flow_ok` (`in`, `Bool`),
aggregated upstream through plant wiring; a non-`Good` permissive —
and a non-`Good` `request_i` — reads fail-safe: not-OK and not
asserted. The grant asserts only while every permissive holds, holds
while the granted filter's request stands, and releases the scan it
drops — completion and abort release identically, so the arbiter
carries no completion vocabulary. The `parameters` are the `Int`
codes `queue_policy` (`0` FIFO, `1` priority-by-trigger — the
coordinator sees only `request_i`, so the plant numbers filters in
priority order — `2` operator-managed, where no grant issues until
the standing reorder instruction selects the queue's head) and
`queued_state` (`0` keep filtering until granted, `1` offline with
standby cover — declared contract data; its effect composes in bank
wiring). Both are required declared data and `SetParameter`-tunable.
Where the model binds `reorder` (`in`, `Int`) — conventionally a
writable internal `in` point, so operator writes ride the journaled
receipted command path and the held value crosses checkpoints — it
carries a standing instruction: a `Good` value in `1..=N` naming a
queued member moves it to the head (and, under `queue_policy` `2`,
selects it for the grant); `0`, out-of-range, non-queued, and
non-`Good` values apply nothing. An instance not exposing reorder
declares no `reorder` port. The ordered queue and the held grant are
per-scan run state: `capture_state` carries `granted`, `queued_count`,
and `queue_k` under decision 20, so a checkpointed standby inherits
order and grant mid-queue.
`crates/dcs-assembly/fixtures/backwash_coordinator.json` is the
recorded composition — a three-filter FIFO bank beside a two-filter
operator-managed bank driving the reorder point; per-port semantics
live beside `BackwashCoordinator::KIND`.

The post-wash verification checks architecture decision 61 records
add one fixed-arity kind. `phase-monitor` reads `in` (`in`, `Float`)
— the measured value — `phase` (`in`, `Bool`) — the condition
window, wired from decoded phase flags or the return-to-service
state — and `capture` (`in`, `Bool`) — while asserted inside the
window the kind tracks `in` as its baseline — and drives `deviation`
(`out`, `Float`), the reported `in − baseline`; `exceeded` (`out`,
`Bool`), the excursion condition the alarm set consumes; and
`overdue` (`out`, `Bool`), the bound-not-met verdict. The
`parameters` are `bound` (non-negative finite `Float`), `limit_ticks`
(`Int`, `1`–`i64::MAX`, the scans of the open window within which the
bound must first be met), and `mode` (`Int` code — `0` absolute
bound on `in`, `1` magnitude bound on the deviation); all three are
`SetParameter`-tunable. The window opens on the scan `phase` reads
`true` and closes on the scan it reads `false`, and each window
carries its own verdict state — scans stood, the latched `met`, the
baseline — reset as it opens. Under `mode` `0`, `exceeded` stands
while `in` reads above `bound` and the first scan within bound
latches `met`; under `mode` `1`, `deviation` reports `in − baseline`
(`0.0` until the window's first capture), `exceeded` bounds the
deviation magnitude, and only a captured deviation can be met — a
window whose `capture` never runs meets nothing. `overdue` asserts on
the first window scan past `limit_ticks` without `met`, and clears
when the bound is met late or the window closes. Outside the window
the monitor is quiescent — `deviation` `0.0`, both flags clear,
nothing accrues, a `capture` read `true` acting on nothing — and the
held baseline drops when the window closes, so a fresh window never
verifies on a stale reference. A scan where any input is not `Good`,
or `in` is not finite, is a held scan: the window neither opens nor
closes, the count stands, nothing captures, and the outputs keep
their standing values stamped with the merged worst-of input
qualities — plus `Bad(DeviceFault)` for a non-finite reading the
point did not report. The baseline, the deadline count, and the
standing verdicts are run state under decision 20's checkpoint rule,
so a checkpointed standby continues a mid-window verification
identically.
`crates/dcs-assembly/fixtures/phase_monitor.json` is the recorded
composition — a mode-0 ripening instance beside a mode-1 CBHL
instance over scripted turbidity, headloss, phase, and capture
channels; per-port semantics live beside `PhaseMonitor::KIND`.

The aeration-header coordination contract architecture decision 62
records adds one variable-arity kind. `header-coordinator` owns the
bank-level coordination the independent per-zone DO→valve loops
cannot express — the shared discharge header lets the loops hunt
each other through the common pressure, so a bank coordinator holds
the coordination strategy and the plant-wide pulse cap. Per zone `i`
declared under the indexed-family convention — the zone count is the
highest bound index and every index below it must bind all four
members: `valve_pos_i` (`in`, `Float`) the zone valve's position
feedback, `airflow_i` (`in`, `Float`) the zone's airflow demand,
`pulsing_i` (`in`, `Bool`) the zone's mixing-pulse request, and
`pulse_grant_i` (`out`, `Bool`) the coordinator's pulse admission.
The bank level reads `pressure` (`in`, `Float`) — the header
transmitter — and drives `pressure_sp` (`out`, `Float`) the emitted
set-point, `blower_demand` (`out`, `Float`) the aggregate capacity
demand, `most_open` (`out`, `Int`) the 1-based index of the zone
whose `valve_pos_i` reads highest — `0` while no zone's position is
trusted, a tie resolving to the lowest index — `at_bound` (`out`,
`Bool`) asserted while the emitted set-point rests at a declared
pressure bound or the demand rests at the airflow floor, and
`pulse_blocked` (`out`, `Bool`) asserted while a pulse request
stands refused by the cap. The `parameters` are the `Int` code
`strategy` (`0` constant header pressure — `pressure_sp` holds the
declared `pressure_hold`; `1` most-open-valve pressure reset —
`pressure_sp` walks once every `adjust_ticks` scans, by the
most-open valve's distance outside the `mov_band_lo`–`mov_band_hi`
band, until the valve settles inside it; `2` direct-airflow control
— `blower_demand` carries the summed zone demands and `pressure_sp`
emits the clamped hold as an inert reference), the finite `Float`s
`pressure_hold` (the held set-point and the walk's initial value),
`pressure_min`/`pressure_max` (the emitted set-point's clamp) and
`mov_band_lo`/`mov_band_hi` (each pair ordered), `adjust_ticks`
(`Int` ≥ 1, the minimum interval between set-point moves),
`min_total_airflow` (finite `Float` ≥ 0, the header-level mixing
floor), and `max_pulsing` (`Int` ≥ 0, the simultaneous pulse-grant
cap); all nine are `SetParameter`-tunable, a retune breaking a bound
pair refused naming the parameter. The non-`Good` rules are the
decision's: a `Good` finite `valve_pos_i` alone may claim the
most-open identity; a non-`Good` or non-finite `airflow_i` holds the
zone's last trusted demand — a zone never trusted contributes
nothing — and `blower_demand` floors at `min_total_airflow`; a
non-`Good` `pulsing_i` reads as not requesting, releasing any held
grant; and a reset-strategy interval landing on a non-`Good` or
non-finite `pressure`, or an empty most-open selection, skips that
move rather than catching up — the held set-point does not step on
an untrusted read. Pulse grants hold while their request stands and
waiting requests admit in ascending zone order as capacity frees.
The emitted set-point, the aggregate demand, the most-open identity,
the adjustment timer, the held per-zone demands, and the grant set
are per-scan run state under decision 20: `capture_state` carries
them so a checkpointed standby resumes the walk without stepping the
header pressure.
`crates/dcs-assembly/fixtures/header_coordinator.json` is the
recorded composition — one bank per declared strategy over one
scripted input set; per-port semantics live beside
`HeaderCoordinator::KIND`.

The blower-staging contract architecture decision 63 records adds one
variable-arity kind. `blower-group` stages `N` blowers against a
continuous `demand` (`in`, `Float` — the `header-coordinator`'s
`blower_demand`), each unit `i` declared under the indexed-family
convention with six members: `cmd_i` (`out`, `Bool`) the group's run
request, `run_i`/`fault_i`/`avail_i` (`in`, `Bool`) the pump-group
equipment family, `capacity_i` (`out`, `Float`) the unit's share of
the demand under the equal split clamped to its declared bounds, and
`vent_i` (`out`, `Bool`) the unloading-vent command — `true` holds the
unit off the header. The bank level reports `staged` (`out`, `Int`)
the commanded count, `none_available`/`all_faulted` (`out`, `Bool`)
the station conditions, `staging_pending` (`out`, `Bool`) a stage
change standing under a non-automatic authority, and `transition`
(`out`, `Bool`) a join, departure, or rotation handover in progress —
the freeze surface the most-open-valve wiring consumes. The
`parameters` are `staging_authority` (`Int`, `0` automatic, `1`
operator-approval — a pending change holds on `staging_pending` until
a `Good` `true` on the bound `approve` point grants it, one assertion
granting one change; the authority requires the bound port — `2`
flag-only, recommendations only), `stage_up`/`stage_down` (finite
`Float`s ≥ 0 — fractions of the committed capacity: `demand` over
`stage_up × committed` stages up, under `stage_down × (committed −
the departing unit's ceiling)` stages down), `min_run_ticks` and
`min_start_interval_ticks` (`Int`s ≥ 0 — the time a joined unit must
serve before it may depart and a stopped unit must sit out before
restart), per-unit `unit_<i>_min_flow`/`unit_<i>_max_flow`/
`unit_<i>_max_current` (finite `Float`s ≥ 0, ordered `min_flow ≤
max_flow` and `min_flow ≤ max_current`, the effective ceiling the
tighter of the two maxima), `vent_ticks` (`Int` ≥ 1, the prove window
both choreography legs get), and `rotation` (`Int`, `0` none — lowest
index in, most recently joined out — `1` equalize runtime — least-run
standby swaps in for the most-run joined once the spread reaches
`max(1, min_run_ticks)`, the join running first and `transition`
asserted across the handover — `2` fixed order — lowest index in,
highest joined index out). The join choreography is the decision's
offline start: `vent_i` opens with `cmd_i`, `run_i` must prove within
`vent_ticks`, then the vent closes; departure runs it in reverse —
`cmd_i` drops behind the open vent, `run_i` proves the stop within
`vent_ticks`, then the vent closes, an unproven stop resolving the
unit offline anyway with its still-running feedback still
accumulating run-hours. A joined unit faulting or losing availability
departs the same scan and its demand hands to the next eligible
standby. The non-`Good` rules are the decision's: `avail_i` non-`Good`
reads unavailable, `fault_i` non-`Good` reads failed, `run_i`
non-`Good` proves neither running nor stopped and accumulates no
run-hours, a non-`Good` or non-finite `demand` holds the last `Good`
value, and a non-`Good` `approve` grants nothing; every output
carries `Good`. Where the model binds `approve` (`in`, `Bool`) —
conventionally a writable internal `in` point, so grants ride the
journaled receipted command path — the operator-approval authority
consumes it; an instance not exposing it declares no `approve` port.
The staging position, phase timers, run-hours, start/stop references,
join order, rotation pair, held demand, and consumed approval are
per-scan run state under decision 20: `capture_state` carries them so
a checkpointed standby resumes a mid-join run identically.
`crates/dcs-assembly/fixtures/blower_group.json` is the recorded
composition — five groups covering every `staging_authority` and
`rotation` code over one scripted device; per-port semantics live
beside `BlowerGroup::KIND`.

The machine-protection demand bound architecture decision 64 records
adds one fixed-arity kind. `surge-guard` sits on each blower's demand
path between the `blower-group`'s `capacity_i` and the machine
actuation demand, bounding `demand` (`in`, `Float`) against the
declared two-variable surge region: `flow` (`in`, `Float`) — the
unit's discharge airflow — below `min_flow`, `pressure` (`in`,
`Float`) — the discharge or header pressure — above `max_pressure`,
or a bound `current` (`in`, `Float`) — the optional minimum-amperage
proxy — below `min_current` sit inside the region, the comparisons
strict so a reading resting on its bound stays clear. `surge_trip`
(`in`, `Bool`) is the hardwired protective device's proven-surge
status — the kind honors it, never re-implements the device. It
drives `out` (`out`, `Float`) — the guarded demand — `guarding`
(`out`, `Bool`) — asserted while any declared bound is crossed — and
`tripped` (`out`, `Bool`) — asserted while a proven surge, a declared
trip response, or an untrusted demand drives `trip_value`. The
`parameters` are `min_flow`, `max_pressure`, and `min_current`
(non-negative finite `Float`s — the declared surge-region bounds),
`on_guard` (`Int` code — `0` clamps `out` at each crossed bound,
`min_flow` and `min_current` flooring the demand and `max_pressure`
capping it; `1` trips `out` to `trip_value`), and `trip_value`
(finite `Float` — the demand a trip emits, the unload/vent direction
the machine's shutdown requires); all five are
`SetParameter`-tunable. Inside the region the demand passes
unmodified; a proven `surge_trip` drives `trip_value` whatever the
demand or the region; neither report latches — the kind auto-clears
with the inputs, and a plant needing a held lockout wires an
`sr-latch` downstream per the decision. An instance not exposing the
amperage proxy declares no `current` port — bound through
`ComponentSpec::get` — and `min_current` guards nothing on it. The
non-`Good` rules are the fail-safe set: an untrusted `flow` reads
below `min_flow`, an untrusted `pressure` above `max_pressure`, an
untrusted bound `current` below `min_current`, a non-`Good`
`surge_trip` reads as proven, and an untrusted or non-finite `demand`
can neither pass nor be bounded, so `out` drives `trip_value` and
`tripped` asserts. `out` carries the merged worst of every bound
input's quality plus `Bad(DeviceFault)` for a non-finite reading the
point did not report; `guarding` and `tripped` always carry `Good`.
The tuned parameters are the only run state under decision 20 —
every output is a pure function of the current inputs — so a
checkpointed standby resumes identically.
`crates/dcs-assembly/fixtures/surge_guard.json` is the recorded
composition — a clamp instance with `current` bound beside a trip
instance without it over one scripted input set; per-port semantics
live beside `SurgeGuard::KIND`.

The declared DO-loss fallback architecture decision 65 records adds
one fixed-arity kind. `demand-fallback` sits on a zone's DO→airflow
demand path — downstream of the `failover-select`/`median-voter`
measurement layer — and carries the terminal response that layer
cannot express once no DO source remains good. It reads `in` (`in`,
`Float`) — the loop's airflow demand — and `pv` (`in`, `Float`) — the
selected DO measurement whose quality drives the fallback — and
drives `out` (`out`, `Float`) — the demand served downstream — and
`fallback_active` (`out`, `Bool`) — asserted for the engagement's
duration, the alarmed transition surface the alarm set consumes. The
`parameters` are `on_bad` (`Int` code — `0` holds the last demand the
kind stamped `Good`, zero before the first; `1` drives `fallback_flow`,
a fixed airflow; `2` drives `safe_flow`, the declared safe airflow —
e.g. the permit-protecting rate for the low-DO direction) and
`fallback_flow`/`safe_flow` (finite `Float`s — the fixed demands the
codes emit, declared engineering data emitted verbatim rather than
clamped); all three are `SetParameter`-tunable where the plant
declares the code operator-selectable. While `pv` reads `Good` with a
finite value the demand passes unmodified; a non-`Good` or non-finite
`pv` — a reading that cannot be controlled on — engages the declared
response the same scan; recovery resumes pass-through the first scan
`pv` reads `Good` again, neither state latching. The held demand is
the last `Good` finite `in` stamped during pass-through — frozen for
the engagement's duration, so a still-`Good` `in` moving while the
fallback stands does not follow it; `in` itself never gates the
response, an untrusted demand under a `Good` `pv` passing verbatim.
`out` carries the merged worst of the `in` and `pv` qualities plus
`Bad(DeviceFault)` for a non-finite reading the point did not report,
so a held or fallback demand stays marked untrusted;
`fallback_active` always carries `Good`. The held demand and the
tuned parameters are run state under decision 20: `capture_state`
carries them so a checkpointed standby resumes an engaged fallback
identically.
`crates/dcs-assembly/fixtures/demand_fallback.json` is the recorded
composition — one instance per `on_bad` code over one scripted input
set; per-port semantics live beside `DemandFallback::KIND`.

The feed-forward demand injection architecture decision 66 records
adds one fixed-arity kind — the additive sibling of the landed
`flow-paced-ratio`, whose `trim` scales multiplicatively where this
kind's adds. `feedforward-sum` sits on a zone's aeration demand path
where a load feed-forward or an ammonia-based supervisory trim *adds
to* the airflow demand rather than scaling it. It reads `ff` (`in`,
`Float`) — the computed feed-forward demand, conventionally a
`flow-paced-ratio` with `trim` unwired where the pacing law is
proportional — and `trim` (`in`, `Float`) — the feedback loop's
additive correction, typically the zone DO `pid`'s `out` — and drives
`out` (`out`, `Float`), the summed demand bounded to `[min_demand,
max_demand]`, `clamped` (`out`, `Bool`), and `fallback_active`
(`out`, `Bool`) — the decision-50 status vocabulary. The `parameters`
are `trim_min`/`trim_max` (finite `Float`s, `trim_min` ≤ `trim_max` —
the feedback portion's declared authority: it trims, never owns, the
demand), `min_demand`/`max_demand` (finite `Float`s, `min_demand` ≤
`max_demand` — the emitted-demand bounds), and the `Int` codes
`on_bad_ff`/`on_bad_trim` (`0` the untrusted term drops out and the
other term serves alone; `1` the term's last `Good` finite value
stands in, zero before the first); all six are
`SetParameter`-tunable. Each scan serves `clamp(ff + clamp(trim,
trim_min, trim_max), min_demand, max_demand)` — the trim bounded
before the sum — with `clamped` asserting while either bound engages;
a term resting exactly on its bound is not a clamp. A non-`Good` or
non-finite input takes its declared response the same scan — each
term independently, so both bad compose their responses — with
`fallback_active` asserted for the engagement's duration and no
latch: the first `Good` finite scan serves and banks again. The
served terms still pass through the same bounds, so `clamped` reports
honestly while a response runs. `out` carries the merged worst of the
`ff` and `trim` qualities plus `Bad(DeviceFault)` for a non-finite
reading the point did not report; `clamped` and `fallback_active`
always carry `Good`. The held last-`Good` terms and the tuned
parameters are run state under decision 20: `capture_state` carries
them so a checkpointed standby resumes an engaged response
identically.
`crates/dcs-assembly/fixtures/feedforward_sum.json` is the recorded
composition — drop/drop, hold/hold, and mixed response pairings over
one scripted `ff`/`trim` set; per-port semantics live beside
`FeedforwardSum::KIND`.

The one-sided derivative annunciation the decision-75 seam map's
recorded gap names adds one fixed-arity kind. `rate-of-rise` reads
`in` (`in`, `Float`) — the measured value — and drives `rate` (`out`,
`Float`), the per-scan first difference in the input's declared
measured-units-per-tick (components own no wall clock — a scan is the
unit), and `rising` (`out`, `Bool`), the standing condition a
downstream `bool-latching-alarm`/`managed-bool-latching-alarm` `in`
consumes. The `parameters` are `rate_limit` (strictly positive finite
`Float`, the per-tick rise the flag asserts at) and `initial_rate`
(finite `Float`, the rate reported until the first `Good` sample pair
completes a difference — a plant choosing the alarm-until-proven
posture declares an initial at or above the bound); both are
`SetParameter`-tunable. Each scan where `in` reads `Good` and finite
banks the sample and, once a pair stands, reports `in − previous` —
signed, so a rising input asserts at the bound while a falling
excursion, however fast, stays silent: the one-sided verdict the
IJmuiden composition's `deviation-monitor`-versus-`signal-filter`
detector cannot express. A difference overflowing `f64` saturates at
`±f64::MAX`. A scan whose `in` is not `Good`, or not finite, freezes
— the previous sample holds, `rate` and `rising` hold their standing
values stamped with the input's quality plus `Bad(DeviceFault)` for a
non-finite reading the point did not report — and the recovery scan
differences against the last banked sample, the whole excursion
across the gap reporting as one per-tick rate. Neither output
latches: `rising` releases the first evaluated scan below the bound,
the alarm kind the flag feeds owning the held annunciation. The
kind is deliberately one-sided — whether a declared `direction`
parameter or a falling sibling covers the other side is the
parameterization record's open item. The banked previous sample, the
standing rate and flag, and the tuned parameters are run state under
decision 20: `capture_state` carries them so a checkpointed standby
differences the next sample identically, no spurious edge.
`crates/dcs-assembly/fixtures/rate_of_rise.json` is the recorded
composition — a silent-initial instance beside a declared-initial
instance asserting until its first `Good` pair, over one scripted
level; per-port semantics live beside `RateOfRise::KIND`.

## `connections`

A list of wires between endpoints. Each connection is
`{"from": <endpoint>, "to": <endpoint>}` where an endpoint is one of:

- `{"point": <io_point id>}` — a logical I/O point;
- `{"port": {"component": <component id>, "name": "<port>"}}` — a named
  port on a component instance.

Validation rules (`PlantModel::validate`, errors carrying the
connection's index in `connections` and the offending end):

- both endpoints resolve: the point exists (`UnknownPoint`), the
  component exists (`UnknownComponent`), the port exists on it
  (`UnknownPort`);
- `from` produces a value — an `in` point or an `out` port — and `to`
  consumes one — an `out` point or an `in` port
  (`ConnectionDirectionMismatch`);
- both ends carry the same `value_type` (`ConnectionTypeMismatch`).

What each shape means at assembly:

- **point → port** and **port → point** bind the port to the io_point —
  field or internal alike. A port may be bound once; a second connection
  naming it is `AssemblyError::PortBoundTwice`.
- **port → port** synthesizes a linked internal `out`/`in` point pair:
  the producing port's writes land in the image and the internal link
  delivers them to the consuming port at the next scan's input phase —
  a component-to-component wire arrives one scan later.
- **point → point** is a wire between two io_points — by the direction
  rules always `from` an `in` point `to` an `out` point, so the `out`
  point's writes feed the `in` point's reads one step later. Both ends
  channel-bound: a field-side `Loopback`, staying inside the shared
  local `SimDriver` when both ends are sim-served and becoming a
  `FanoutDriver` route across backends otherwise. Both ends internal:
  an internal link in the scan image at the same one-scan-later
  boundary. One end field-bound and one internal cannot be carried:
  `AssemblyError::MixedPointLink`.

`crates/dcs-assembly/fixtures/tank_loop.json` wires `analog-input` and
`pid` instances through all three shapes;
`crates/dcs-assembly/fixtures/internal_points.json` shows declared
internal-point wiring.

## Device kinds and their `parameters`

`Device.kind` resolves through the deployment's `DriverRegistry`;
`DriverRegistry::standard()` (`crates/dcs-assembly/src/drivers.rs`)
installs the built-in set: exact registrations for `sim-tcp`, `sim-bus`,
`sim-scripted`, and `ethercat`, plus the `sim` prefix serving every other
`sim*` name (`sim` itself and role-flavored kinds like `sim-8ai`,
`sim-4ao`, `sim-ai`, `sim-ao` — the convention `dcs-demo` established).
Exact registrations always win over the prefix, which is how the other
exact kinds route to their own backends.

Every factory receives the device's declared `channels`, its `hardware`
marker, its `parameters`, and the `io_point`s bound to those channels as
`DevicePoint`s — already guaranteed by validation to agree on direction
and value kind — and must serve exactly those points. A rejected
parameter is `AssemblyError::InvalidDeviceParameters` naming the device
and kind; a backend that cannot be built or cannot serve a declared
point is `AssemblyError::DeviceBackend`.

### `sim*` — local simulated device

Takes **no parameters** — a `sim*` device declaring any parameter key is
`InvalidDeviceParameters`. Each bound io_point joins the shared local
simulated `ChannelMap` as a `PointBinding` starting at the neutral value
of its kind (`false`, `0`, `0.0`); all `Sim` contributions merge into
one `SimDriver` backend, so a model can mix many `sim*` devices freely.
The local backend is not field-facing: a redundant peer's tracking copy
steps its own, and no write-ownership claim applies.

### `sim-tcp` — remote simulated plant

A `dcs_sim_net::RemoteDriver` attached to a `dcs-plant-server`'s shared
simulated plant over TCP. Parameters:

- `"address"` — required string: the plant server's `host:port`, which
  must resolve to at least one socket address;
- `"timeout_ms"` — optional non-negative integral number of
  milliseconds, defaulting to `RemoteDriver::DEFAULT_TIMEOUT` (5 s).

Any other parameter key is rejected. The factory connects eagerly — an
unreachable endpoint is `DeviceBackend`, not a mid-scan surprise — and
reads every declared point once, failing when the remote plant does not
serve it or serves it with a different value kind. The backend is
field-facing and installs the plant server's single-writer claim: the
fencing a promoted redundant peer takes out on the old field owner
(decision 28).

`crates/dcs-assembly/fixtures/mixed_kinds.json` shows a `sim-tcp` device
beside a local `sim` one.

### `sim-scripted` — tick-indexed playback

A `dcs_sim::ScriptedDriver` whose `in` channels replay a declared script
and whose `out` channels record every accepted write (readable through
`FanoutDriver::inspect` as the `ScriptedDriver`'s `writes` log).
Parameters: exactly one entry, `"script"` — required, object-shaped:
channel name → array of entries. Each entry is an object with keys:

- `"tick"` — required non-negative integer: the driver tick the entry
  takes effect at, holding until the next entry;
- `"value"` — required plain JSON scalar matching the channel's
  `value_type`: a JSON bool for `bool`, an integer for `int`, a finite
  number for `float`;
- `"quality"` — optional `"good"` (the default), `"uncertain"`, or
  `"bad"`;
- `"reason"` — optional `snake_case` `QualityReason` name:
  `unspecified`, `substituted`, `stale`, `out_of_range`,
  `communication_fault`, `device_fault`, `configuration_fault`.
  Meaningful only on a non-`"good"` entry — `"reason"` with
  `"quality": "good"` is an error — and defaults to `unspecified`.

Rules the factory enforces, all as `InvalidDeviceParameters`:

- an entry object may carry only the four keys above — anything else is
  rejected;
- a script may name only `in` channels the device declares, and only
  channels an io_point binds;
- each channel's entries must be in strictly increasing tick order; an
  entry at tick `0` is already in effect when the driver is built;
- an unscripted `in` channel serves its neutral `Good` value.

The backend is not field-facing — there is no shared field to claim.
`crates/dcs-assembly/fixtures/scripted_kinds.json` is a worked example.

### `sim-bus` — register-mapped simulated fieldbus

A `dcs_sim_bus::BusDriver` reaching a `dcs-sim-bus-device` server's
register bank over TCP — a second transport shape addressed by register
index. The parameter grammar lives in
`crates/dcs-sim-bus/src/params.rs` (`DeviceParameters::parse`), and the
device-server binary parses the same declaration when it serves the
registers, so a rig's two ends cannot diverge. Parameters:

- `"address"` — required string: the device server's `host:port`, and
  the server binary's default `--listen`;
- `"timeout_ms"` — optional non-negative integral number of
  milliseconds, defaulting to `BusDriver::DEFAULT_TIMEOUT` (5 s);
- `"registers"` — required object mapping channel name → register
  declaration. An entry is either a bare register index
  (`"level-raw": 4`) or an object `{"register": <u16>, "initial":
  <tagged Value>}` whose optional `initial` seeds the server register —
  the neutral value of the channel's kind when absent — and whose
  variant must match the channel's `value_type`. The map must cover the
  declared channel set exactly — it may not name an undeclared channel
  nor miss a declared one — and no two channels may share a register.
  Register addresses fit `0..=65535`.

Any other parameter key is rejected. The factory connects eagerly and
probes each mapped register — an unreachable endpoint, a register the
server does not hold, or a kind disagreement is `DeviceBackend` at
assembly. The backend is field-facing but declares no write-ownership
claim: `FanoutDriver::unfenced_field_devices` names `sim-bus` devices,
and `dcs-controller --auto-promote` refuses a model built on one —
manual promotion only (decision 28).

`crates/dcs-assembly/fixtures/mixed_bus.json` shows a `sim-bus` device
beside a local `sim` one.

### `ethercat` — hardware-bound cyclic field-bus

An EtherCAT coupler or remote-I/O station — the first *hardware-bound*
kind. The device declares `"hardware": true`, and the kind's factory
requires the marker: a model omitting it is `InvalidDeviceParameters`,
and a `sim*` device carrying the marker is rejected for the symmetric
reason. The parameter grammar lives in `crates/dcs-ethercat/src/
params.rs` (`DeviceParameters::parse`); parameters:

- `"bus"` — required non-empty string: the *logical* bus name. The
  model names the bus; **deployment configuration binds the name to a
  host interface outside the document** (decision 47) — no parameter
  names a host interface, so a plant model stays identical wherever the
  controller runs. `crates/dcs-assembly/tests/ethercat.rs` pins that
  the declared vocabulary admits no interface field;
- `"identity"` — required object `{"vendor": <u32>, "product": <u32>,
  "revision": <u32>}`: the expected station identity the master checks
  the answering station against before outputs are enabled;
- `"mapping"` — required object `{"inputs": {...}, "outputs": {...}}`:
  the channel → process-data-offset layout. Each direction's image
  places every declared channel of that direction — `{"byte": <u32>,
  "bit": <0–7>}` for a `bool` channel, or a byte-aligned `{"byte":
  <u32>, "bits": <width>}` field for an `int` channel (8, 16, 32, or 64
  bits) or a `float` channel (32 or 64 bits). No two channels' bit
  ranges may overlap inside an image;
- `"exchange_miss_threshold"` — required integer ≥ 1: consecutive
  failed cyclic exchanges before the device's reads escalate to
  `IoError::Disconnected` under the decision-78 cyclic contract;
- `"safe_outputs"` — object channel → tagged `Value`, required when the
  device declares `out` channels: every `out` channel's declared safe
  state, matching the channel's `value_type`, staged into the output
  image before the first exchange;
- `"startup"` — required object `{"on_mismatch": "fail"}`: the only
  policy the contract admits. A station identity or layout mismatch is
  a **hard startup failure** — a hardware-bound kind is never silently
  substituted by simulation and never runs degraded against a station
  that does not match the declaration.

Any other parameter key is rejected. A malformed declaration —
missing `bus`, a mistyped identity, colliding offsets, a channel placed
in the wrong image or left unmapped, a bad threshold, a non-`fail`
startup policy, a missing or kind-mismatched safe state — is
`InvalidDeviceParameters` before any scan. Until the EtherCAT master
integration lands (Lenovo QA lane HQ-4), a *well-formed* declaration
still fails assembly as `DeviceBackend`: no bus can initialize, and the
kind fails startup rather than substituting a simulated backend. The
emitted JSON Schema carries the kind-conditional shape for the keys it
can express; the channel-table-dependent rules stay with the factory.

`dcs-build` composes the same declaration through
`dcs_build::ethercat` — `EthercatSpec` carries the bus, identity, and
miss threshold, and `ethercat_input`/`ethercat_output` declare each
channel with its `ImageOffset` (and, for outputs, its safe state), so a
mistyped offset is a compile-time-adjacent panic rather than an
assembly error. `crates/dcs-assembly/fixtures/ethercat.json` is the
reference document the builder's test emits byte-for-byte.

## The dynamics document

The dynamics document is a second, deliberately separate declaration:
the testbench's account of the process physics the simulated plant
stands in for (decision 24). It is *not* model schema — a real plant has
no such declaration, so the model describes what the controller sees and
the dynamics document describes what the world does. It is a plain JSON
list with no `version` field; `dcs-plant-server --dynamics FILE` merges
it into the resolved `sim_channel_map`, and test rigs do the same by
adding `ProcessElement`s to a `ChannelMap` before constructing
`SimDriver`.

Each list entry is one `dcs_sim::ProcessElement` — an externally tagged
object whose single key is the snake_case element name:

| Tag | Fields | Behavior |
|---|---|---|
| `first_order_lag` | `input`, `output`, `time_constant`, `initial` | `dy/dt = (u − y) / time_constant`. |
| `second_order_lag` | `input`, `output`, `time_constant`, `damping_ratio`, `initial` | `τ²y″ + 2ζτy′ + y = u` with `time_constant` = τ and `damping_ratio` = ζ — ζ < 1 underdamped (overshoots), ζ = 1 critical, ζ > 1 overdamped; the output starts at `initial` at rest. Stepped by the exact zero-order-hold discretization. |
| `integrator` | `input`, `output`, `initial` | `dy/dt = u`. |
| `dead_time` | `input`, `output`, `delay`, `initial` | `y(t) = u(t − delay)`; the realized delay rounds up to whole steps and the output holds `initial` until the delay line has filled. |
| `noise` | `input`, `output`, `amplitude`, `seed`, `initial` | `y = u + amplitude·(2x − 1)`, `x` drawn once per step from a splitmix64 generator seeded by `seed` — the output stays within `u ± amplitude` and identical seeds replay identical deviation sequences. |
| `bool_flow` | `input`, `output`, `on_rate`, `off_rate`, `initial` | `y = on_rate` while the gate reads `true`, `off_rate` while it reads `false` — a bool-gated flow source answering an actuator's run command. `input` is the one non-float element end: a `bool` point. Rates are signed flows — a negative `on_rate` is a pump's draw — and `dt` does not scale them; a downstream `integrator` owns the time base. |
| `flow_sum` | `inputs`, `output`, `bias`, `initial` | `y = bias + Σ inputs` over a declared list of `float` points — how an inflow and per-pump draws combine into one net rate. `bias` is a constant term (a declared inflow needs no point of its own) and may be omitted, deserializing as zero; an empty `inputs` declares exactly a constant. `dt` does not scale the sum. |
| `scaled_flow` | `input`, `output`, `gain`, `initial` | `y = gain · u`, re-evaluated each step — a `float` demand scaled into the signed rate a downstream `flow_sum` or `integrator` consumes: a metering pump's measured discharge at its analog speed demand, a chemical tank's drawdown under a negative `gain`. `dt` does not scale the output. |
| `threshold` | `input`, `output`, `on`, `off`, `initial` | The `float`→`bool` element — `bool_flow`'s mirror: a `float` `input` driving a `bool` contact `output` that asserts and releases on the declared `on`/`off` bounds. `on > off` is a high trip — assert at `u ≥ on`, release strictly below `off`; `on < off` is a low trip — assert at `u ≤ on`, release strictly above `off`; between the bounds the contact holds, so the band is the hysteresis that keeps a noisy input from chattering the output. `initial` is a `bool` covering reads before the first `Good` step, and `dt` scales nothing — the contact is a pure function of the standing input at each tick boundary. A level, pressure, or temperature crossing can thus drive protective behavior — the contact gating a `bool_flow` emergency draw — with no scheduled script. |

Common rules, enforced by `ChannelMap::validate` as each element merges
(`dcs-plant-server` reports a failure naming the element's index and the
point it drives):

- `input`/`inputs` and `output` are io_point ids (`PointId`s) naming
  points the served map binds — channel-bound io_points on `sim*`
  devices; channel-less internal points are image-carried and
  unreachable for elements (`ConfigError::UnknownPoint`);
- element ends must be `float` points (`ElementPointKind`) — except a
  `bool_flow`'s gate `input`, which must be a `bool` point
  (`ElementGateKind`), and a `threshold`'s contact `output`, which must
  be a `bool` point (`ElementContactKind`);
- `time_constant`, `damping_ratio`, and `delay` must be finite and
  positive (`InvalidTimeConstant`, `InvalidDamping`, `InvalidDelay`),
  `amplitude` finite and non-negative (`InvalidAmplitude`), `on_rate`,
  `off_rate`, `gain`, and `bias` finite (`InvalidRate`, `InvalidGain`,
  `NonFiniteBias` — rates and gains are signed, so a negative draw is
  legal), `on` and `off` finite and distinct (`InvalidBound`,
  `NonPositiveBand` — equal bounds declare no hysteresis band), and
  `initial` finite (`NonFiniteInitial`);
- a non-`Good` input freezes the element's state and propagates its
  quality to the output sample — a `flow_sum` propagating the worst of
  its inputs' qualities, a `threshold` holding its standing contact;
- no point may be driven by more than one loopback or element
  (`ConflictingDriver`);
- elements step in declaration order, after loopback routing, so an
  element observes the input value the current step produced.

Example — `crates/dcs-plant/fixtures/tank_loop_dynamics.json`, a
first-order lag driving the tank-level measurement from the valve
command:

```json
[
  {
    "first_order_lag": {
      "input": 20,
      "output": 10,
      "time_constant": 2.0,
      "initial": 4.0
    }
  }
]
```

`crates/dcs-plant/fixtures/tank_loop_second_order_dynamics.json` shows
`second_order_lag`; `crates/dcs-demo/fixtures/showcase_dynamics.json` is
the showcase plant's; `crates/dcs-sim/fixtures/pump_station_dynamics.json`
is the station loop `bool_flow` and `flow_sum` exist for — two bool-gated
pump draws and a declared inflow summed into an integrator driving the
well level; and `crates/dcs-sim/fixtures/dosing_skid_dynamics.json` is
the dosing loop `scaled_flow` exists for — the metering pump's analog
speed demand scaled into the measured discharge rate and, with a
negative gain, the chemical tank's drawdown, integrated into the tank
level — merging onto `crates/dcs-plant/fixtures/dosing_skid.json`'s
points. `crates/dcs-sim/fixtures/protection_dynamics.json` is the
protection loop `threshold` exists for — the level crossing asserting
the `sis-active` contact that gates a `bool_flow` emergency draw —
merging onto `crates/dcs-plant/fixtures/protection.json`'s points.

## Which layer checks what

The pipeline is: parse and version check → structural validation →
assembly → advisory lint. A document must clear each stage before the
next sees it; lint never blocks anything.

| Stage | Runs | Catches |
|---|---|---|
| Parse | `PlantModel::load` | `LoadError::Malformed` — broken JSON or a field whose shape does not match the schema |
| Version | `PlantModel::load` | `LoadError::UnsupportedVersion` — `version` other than `MODEL_VERSION` |
| Validation | `PlantModel::validate` (inside `load`, or standalone for programmatically built models) | Every `ValidationError`, all reported together: `DuplicateId`, `UnknownDevice`, `UnknownChannel`, `ChannelDirectionMismatch`, `ChannelTypeMismatch`, `FieldInitial`, `MissingInitial`, `InitialKindMismatch`, `WritableOut`, `UnknownSource`, `UnknownPoint`, `UnknownComponent`, `UnknownPort`, `ConnectionDirectionMismatch`, `ConnectionTypeMismatch` |
| Assembly | `dcs_assembly::resolve_drivers` + `DriverPlan::build` + `assemble` | Every `AssemblyError`: kind resolution — `UnknownDeviceKind`, `UnknownComponentKind`; device `parameters` and backends — `InvalidDeviceParameters`, `DeviceBackend`; the merged local sim map's consistency — `InvalidChannelMap` (`ConfigError`); port wiring — `PortBoundTwice`, `UnboundPort`; requirement verification — `UnmappedPoint`, `DirectionMismatch`, `TypeMismatch`; constructor failures — `Component`; executor wiring — `Wiring`; and `UnroutedPoint`, `InvalidInternalPoint`, `MixedPointLink`, `UnresolvedEndpoint`, the mirrors reachable for a model assembled without validation |
| Lint | `PlantModel::lint`, `dcs-model lint` | Advisory `LintFinding`s over a validated document, in rule order: `point_without_signal`, `signal_missing_unit`, `signal_missing_description`, `signal_missing_group`, `writable_field_point`, `unbound_channel`. Findings exit zero unless `--strict`; a document failing validation is never linted |
| Dynamics merge | `dcs-plant-server --dynamics`, or a rig extending a `ChannelMap` | A malformed document is a startup parse error; each element's merge is `ChannelMap::validate` — `ConfigError` naming the element index and the point it drives |

The deliberate split: the model validates structure — ids, references,
direction and type agreement, the internal-point and writable rules —
because those hold regardless of deployment. Assembly checks everything
that depends on the registered kinds: which `kind` strings exist, what
`parameters` each kind accepts, whether every port is bound, and whether
each constructed component's declared I/O matches the point map. Lint
checks engineering completeness the contract does not require — signals
for points, display metadata, the writable field surface, dead channel
declarations. Runtime failures are a different surface entirely: a
component `step` error lands in `ComponentDiagnostics`, and driver
problems surface as `IoError`s — never as document errors.

## Authoring and checking a document

- `dcs-model validate <file>` prints every validation error and exits
  nonzero on failure; `summary`, `signal-index`, `diff <old> <new>`
  (`--json` for tooling), and `lint [--strict]` serve the review loop.
- `dcs-build` (`dcs_build::PlantBuilder` plus the per-kind specs in
  `dcs_build::specs`) composes the same document in Rust: `connect` and
  `bind` check port direction and `ValueKind` at compile time where the
  kind's spec is statically known, and `build()` emits the versioned
  document `dcs-model` loads — the builder is a producer of this one
  contract, not a second schema (decision 31).
- `PlantModel::fingerprint` is the model's identity: a hash over the
  canonical reserialized document, insensitive to key order and
  whitespace, stamped into checkpoints so a standby verifies it tracks a
  run of the same model.
