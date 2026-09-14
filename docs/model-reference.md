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
- **`value_type`** is `"Bool"`, `"Int"`, or `"Float"` — the `ValueKind`
  variant names from `dcs-core`.
- **A typed `Value`** — an io_point's `initial`, a component's
  `parameters` entry, a `sim-bus` register's `initial` — serializes as
  an externally tagged object: `{"Bool": true}`, `{"Int": -42}`, or
  `{"Float": 2.5}`. (`sim-scripted` script entries are the one
  exception: their `value` is a plain JSON scalar — see that kind's
  section.)
- **Optional fields** extend the version-1 schema without a version bump
  (decision 3): a document predating a field loads with the field unset,
  and an unset field serializes back without the key. `parameters` on a
  device, `channel`/`initial`/`writable` on an io_point, and
  `unit`/`description`/`group` on a signal all follow this convention.
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
| `channels` | object: name → `{"direction": "in"\|"out", "value_type": "Bool"\|"Int"\|"Float"}` | Required; may be empty. A channel is a physical endpoint an io_point binds to. A channel no io_point binds is lint `unbound_channel`, not an error. |
| `parameters` | object: name → arbitrary JSON | Optional. Kind-specific addressing and configuration, opaque to the model — the registered device-kind factory owns all validation at assembly (`AssemblyError::InvalidDeviceParameters`). Unlike component parameters these are general JSON values, so a kind can carry strings and structured addressing data. |

Example:

```json
{
  "id": 1,
  "kind": "sim-8ai",
  "channels": {
    "ch0": { "direction": "in", "value_type": "Float" }
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
| `value_type` | `"Bool"` \| `"Int"` \| `"Float"` | Required; drivers reject mismatched writes. |
| `channel` | `{"device": <device id>, "name": "<channel>"}` | Optional. Present → a field point: the device must be declared (`ValidationError::UnknownDevice`), the channel must exist on it (`UnknownChannel`), and the point's `direction` and `value_type` must agree with the channel's (`ChannelDirectionMismatch`, `ChannelTypeMismatch`). Absent → an internal point. |
| `initial` | tagged `Value`, e.g. `{"Float": 25.0}` | Optional; required when `channel` is absent (`MissingInitial`), and its variant must equal `value_type` (`InitialKindMismatch`). Forbidden when `channel` is present (`FieldInitial`) — the field owns a bound point's value. |
| `writable` | bool | Optional; unset means not writable. Valid on `in` points only — `writable` on an `out` point is `ValidationError::WritableOut`. |
| `stale_after_ticks` | u64 | Optional; unset means no freshness check. Valid on field-bound `in` points only — on an `out` point it is `ValidationError::StaleOut`, on a channel-less internal point `StaleInternal`. |

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
| `ports` | object: name → `{"direction": "in"\|"out", "value_type": "Bool"\|"Int"\|"Float"}` | Required; may be empty. The signature the model wires; `connections` bind ports to points or other ports. Assembly requires every declared port bound exactly once (`UnboundPort`, `PortBoundTwice`), and the constructed component's declared `IoRequirement`s are checked against the resolved point map — `UnmappedPoint`, `DirectionMismatch`, `TypeMismatch`. |

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
(`out`, `Bool`) asserted while the backup serves. The pair wires
`failover-select.out` onto `threshold-chain.level` through a linked
point pair — an internal link carries quality, a field loopback does
not — and `threshold-chain.demand` onto `pump-group.demand` likewise.
`crates/dcs-assembly/fixtures/station_level.json` is the recorded
composition, manual-takeover gates included; per-port semantics live
beside `ThresholdChain::KIND` and `FailoverSelect::KIND`.

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
and `sim-scripted`, plus the `sim` prefix serving every other `sim*`
name (`sim` itself and role-flavored kinds like `sim-8ai`, `sim-4ao`,
`sim-ai`, `sim-ao` — the convention `dcs-demo` established). Exact
registrations always win over the prefix, which is how the three exact
kinds route to their own backends.

Every factory receives the device's declared `channels`, its
`parameters`, and the `io_point`s bound to those channels as
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
  `value_type`: a JSON bool for `Bool`, an integer for `Int`, a finite
  number for `Float`;
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
| `bool_flow` | `input`, `output`, `on_rate`, `off_rate`, `initial` | `y = on_rate` while the gate reads `true`, `off_rate` while it reads `false` — a Bool-gated flow source answering an actuator's run command. `input` is the one non-Float element end: a `Bool` point. Rates are signed flows — a negative `on_rate` is a pump's draw — and `dt` does not scale them; a downstream `integrator` owns the time base. |
| `flow_sum` | `inputs`, `output`, `bias`, `initial` | `y = bias + Σ inputs` over a declared list of `Float` points — how an inflow and per-pump draws combine into one net rate. `bias` is a constant term (a declared inflow needs no point of its own) and may be omitted, deserializing as zero; an empty `inputs` declares exactly a constant. `dt` does not scale the sum. |

Common rules, enforced by `ChannelMap::validate` as each element merges
(`dcs-plant-server` reports a failure naming the element's index and the
point it drives):

- `input`/`inputs` and `output` are io_point ids (`PointId`s) naming
  points the served map binds — channel-bound io_points on `sim*`
  devices; channel-less internal points are image-carried and
  unreachable for elements (`ConfigError::UnknownPoint`);
- element ends must be `Float` points (`ElementPointKind`) — except a
  `bool_flow`'s gate `input`, which must be a `Bool` point
  (`ElementGateKind`);
- `time_constant`, `damping_ratio`, and `delay` must be finite and
  positive (`InvalidTimeConstant`, `InvalidDamping`, `InvalidDelay`),
  `amplitude` finite and non-negative (`InvalidAmplitude`), `on_rate`,
  `off_rate`, and `bias` finite (`InvalidRate`, `NonFiniteBias` — rates
  are signed flows, so a negative draw is legal), and `initial` finite
  (`NonFiniteInitial`);
- a non-`Good` input freezes the element's state and propagates its
  quality to the output sample — a `flow_sum` propagating the worst of
  its inputs' qualities;
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
is the station loop `bool_flow` and `flow_sum` exist for — two Bool-gated
pump draws and a declared inflow summed into an integrator driving the
well level.

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
