# Integration guide: component kinds and device kinds

The platform has two extension seams, both keyed by `kind` strings the plant
model treats as opaque names:

- **Component kinds** add reusable control logic. A kind is a Rust type
  implementing `dcs_runtime::Component`, built per model instance by a
  constructor registered in a `dcs_assembly::ComponentRegistry`.
- **Device kinds** add I/O backends. A kind is a driver implementing
  `dcs_core::IoDriver`, built per model device by a factory registered in a
  `dcs_assembly::DriverRegistry`.

Once a kind is registered, instantiating it in a model is data, not code:
assembly resolves the declaration, checks the wiring, and the run obtains
control behavior, per-instance diagnostics, a `ComponentDescriptor` the UI
renders, and monitoring access without per-layer engineering. This guide walks
each path end to end, then records what the platform supplies automatically
versus what an integration must implement.

Assembly closes both seams before the first scan:

```text
PlantModel::load            (parse + version check + validate)
    |
    |-- resolve_drivers(&model, &DriverRegistry) -> DriverPlan
    |       devices[].kind -> factory -> DeviceDriver::Sim | DeviceDriver::Backend
    |       DriverPlan::build() -> FanoutDriver (one IoDriver over all backends)
    |
    `-- assemble(&model, &ComponentRegistry, &driver) -> Executor
            components[].kind -> constructor -> Box<dyn Component>
            declared I/O verified against the resolved PointMap
```

Every failure in this pipeline is a structured `dcs_assembly::AssemblyError`
naming the offending device, component, port, or point — integration mistakes
surface at load time, never mid-scan.

## Adding a component kind

The checked-in reference is `dcs_blocks::Timer`
(`crates/dcs-blocks/src/timer.rs`), the smallest kind exercising every piece:
typed I/O, parameters, a descriptor, and checkpointable state.

### 1. Implement `Component`

`dcs_runtime::Component` (`crates/dcs-runtime/src/component.rs`) requires:

- `name()` — the instance's stable identity, used in diagnostics and
  checkpoint keys.
- `io_requirements()` — the declared logical I/O (see below).
- `step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError>`
  — one scan's work. `step` must be a pure function of the component's state,
  its I/O, and `tick`: reading a wall clock or a random source breaks the
  determinism the executor, checkpoints, and hot-swap rely on.

`step` receives a scoped `ComponentIo` view serving exactly the declared
points: reading an undeclared point — or a declared point in the wrong
direction — is `IoError::UnknownPoint`, and writing the wrong `Value` variant
is `IoError::TypeMismatch`. Use `ComponentIoExt::read_typed::<T>` /
`write_typed::<T>` for strict typed access, and `ComponentIo::write_sample`
when an output should carry an input's degraded quality rather than `Good`.

A returned error does not stop the scan: the executor records it against the
component (`ComponentDiagnostics.step_errors` / `last_error` in the
`TelemetrySnapshot`) and continues, and the component's outputs keep their
last written values.

### 2. Declare typed logical I/O

`IoRequirement::input::<T>(name, point)` and `IoRequirement::output::<T>`
declare each port; `T` is `bool`, `i64`, or `f64` (the `PointType` set is
sealed) and fixes the port's `ValueKind`. The requirement `name` is the port
name the model wires and the descriptor reports — keep it identical across
`io_requirements`, the registration's `spec.require(...)` calls, and the
model's `ports` keys.

### 3. `KIND` and `from_parameters`

The `dcs-blocks` convention: a `pub const KIND: &'static str` names the model
`kind` string, and `from_parameters(name, points..., &Parameters) ->
Result<Self, ParameterError>` builds the component from the instance's
parameter map. `Parameters` is `BTreeMap<String, dcs_core::Value>`; failures
are `ParameterError::Missing` or `ParameterError::Invalid`, both naming the
component and the parameter. `TryFrom<Value>` provides the strict conversions
(`bool`, `i64`, `f64`); `dcs-blocks`' kinds add small readers for repeated
shapes (finite `f64`, non-negative `u64`, `Bool`-or-`0`/`1`).

Which points a component binds is *not* a parameter — point ids arrive from
the model's connections through the `ComponentSpec`, separately from
`parameters`.

### 4. `describe()` — self-description for the UI

The default `Component::describe` derives a correct but unannotated
descriptor (name as label, Rust type name as kind, declared I/O as role-less
ports, no parameters). Override it so the monitoring UI can render a real
faceplate: `dcs_blocks::describe::component` builds the descriptor from
`io_requirements()` — so port names, directions, and kinds cannot drift —
plus named `PortRole` hints (`ProcessValue`, `Setpoint`, `Output`, `Status`)
and `describe::parameter` entries matching the keys `from_parameters` reads,
with `ParameterRange` bounds from the shared constants (`FINITE_F64`,
`POSITIVE_F64`, `NONNEGATIVE_F64`, `NONNEGATIVE_INT`). The executor serves one
descriptor per component in `TelemetrySnapshot.descriptors`. The descriptor
is also where a kind declares native commands and emitted events
(`descriptor.commands`/`descriptor.events`) — see *Declared commands and
emitted events* below.

### 5. Checkpoint state

Any value `step` carries between scans belongs in `capture_state()` as a
`StateMap` field, with `restore_state()` validating the whole map before
applying it (`ensure_known_fields`, `require_f64`/`require_i64`/`optional_*`,
`StateError` naming the element and field). The field names are a de facto
wire contract between redundant peers. Stateless kinds leave the defaults.

Checkpointed component state crosses only between same-model peers: a
rolling model revision (`dcs-controller --revised`, decision 25 in
`docs/architecture.md`) reassembles the standby under the new model and
reinitializes every component — the carryover rule moves operator-writable
internal points, still-declared output image samples, and the force set
matched by declared point identity, and names each component's captured
state `DroppedElement::Component` in the carryover report rather than
restoring it. Runtime tuning reverts with the rest of the component
state: the report's `reverted_tuning` itemizes, per reinitialized
component, each descriptor-declared parameter whose checkpointed value
differed from the revision's declared default — the witnessed record of
what a receipted `set_parameter` tune did not carry. A revision that
wants an instance's state to survive keeps
its declared identity (`<kind>:<id>`) — but only ordinary same-model
checkpoint convergence restores it; there are no per-kind compatibility
rules yet.

### 6. Register the kind

A `ComponentRegistry` maps each `kind` string to a constructor receiving a
`ComponentSpec`:

- `spec.name` — the diagnostic name `"<kind>:<id>"` assembly assigns;
- `spec.parameters` — the instance's parameter map;
- `spec.require("port")` / `spec.get("port")` — the `PointId` the model bound
  to a port (`BuildError::UnboundPort` when required and unwired);
- `spec.point_kind(point)` — the bound point's value kind, for kinds with
  type-parameterized variants (`dcs-controller` dispatches `AnalogInput` on
  `i64` vs `f64` this way);
- wrap construction failures with `BuildError::other` so they surface as
  `AssemblyError::Component` naming the instance.

The checked-in example is `dcs_controller::registry()`
(`crates/dcs-controller/src/lib.rs`), which registers every `dcs-blocks`
kind the shipped controller deploys.

A registered kind also carries a `dcs-build` spec
(`crates/dcs-build/src/specs.rs`): a data mirror of the interface
`describe()` reports — kind string, ports in `io_requirements` order,
parameters in `describe` order — that the Rust composition seam checks
at compile time. The kind's `KIND` joins `dcs_blocks::KINDS`, the
checked-in list `crates/dcs-blocks/tests/spec_drift.rs` pins the spec
table against, so a kind added without its spec fails the drift test.
Where a parameter set is not statically enumerable — the sequencer's
`step_<n>_*` keys — the spec records `declared_parameters() -> None`
and documents the treatment.

### 7. Instantiate in a model

A `components` entry declares `id`, `kind`, `parameters` (each a typed
`{"float": …}` / `{"int": …}` / `{"bool": …}` object), and the `ports`
signature the model wires; `connections` bind ports to `io_point`s or to
other ports. Validation (`dcs-model`) enforces that a `from` end produces
(`In` point or `Out` port) and a `to` end consumes (`Out` point or `In`
port), and that both ends agree on value kind; assembly enforces that every
declared port is bound exactly once and that each component's requirements
match the point map. A port-to-port connection synthesizes an internal point
pair, delivering the value one scan later. See the checked-in
`crates/dcs-assembly/fixtures/tank_loop.json` for a complete example wiring
`analog-input` and `pid` instances.

### Worked example: a new component kind, end to end

This example registers a new `running-max` kind — a component tracking the
largest value its `in` port has held since `reset` last read `true` — then
instantiates it in a model document, assembles, and scans it against the
local simulated device. It compiles and runs as part of the test suite.

```rust
use dcs_assembly::{assemble, resolve_drivers, BuildError, ComponentRegistry, DriverRegistry};
use dcs_blocks::{describe, ParameterError, Parameters};
use dcs_core::{
    ComponentDescriptor, IoDriver, PointId, PortRole, Sample, StateError, StateMap, Tick, Value,
    ValueKind,
};
use dcs_model::PlantModel;
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// The kind: the largest `in` since `reset` last read true.
struct RunningMax {
    name: String,
    input: PointId,
    reset: PointId,
    output: PointId,
    max: f64,
}

impl RunningMax {
    /// The model `kind` string the registry maps onto `from_parameters`.
    const KIND: &'static str = "running-max";

    /// Reads the kind's one parameter: `initial` — a required `Float`
    /// (or losslessly representable `Int`) seeding the running maximum.
    fn from_parameters(
        name: impl Into<String>,
        input: PointId,
        reset: PointId,
        output: PointId,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let initial = match parameters.get("initial") {
            None => {
                return Err(ParameterError::Missing {
                    component: name,
                    parameter: "initial".to_string(),
                })
            }
            Some(&value) => f64::try_from(value).map_err(|_| ParameterError::Invalid {
                component: name.clone(),
                parameter: "initial".to_string(),
                detail: format!("expected a Float or Int parameter, found {value:?}"),
            })?,
        };
        Ok(Self {
            name,
            input,
            reset,
            output,
            max: initial,
        })
    }
}

impl Component for RunningMax {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("in", self.input),
            IoRequirement::input::<bool>("reset", self.reset),
            IoRequirement::output::<f64>("out", self.output),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let input = io.read_typed::<f64>(self.input)?;
        let reset = io.read_typed::<bool>(self.reset)?;
        self.max = if reset.value {
            input.value
        } else {
            self.max.max(input.value)
        };
        io.write_sample(
            self.output,
            Sample::new(Value::Float(self.max), input.quality, tick),
        )?;
        Ok(())
    }

    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[("in", PortRole::ProcessValue), ("out", PortRole::Output)],
            vec![describe::parameter(
                "initial",
                ValueKind::Float,
                Some(describe::FINITE_F64),
            )],
        )
    }

    fn capture_state(&self) -> StateMap {
        let mut state = StateMap::new();
        state.insert("max", Value::Float(self.max));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(&self.name, &["max"])?;
        self.max = state.require_f64(&self.name, "max")?;
        Ok(())
    }
}

// Register the kind: the constructor maps the instance's bound ports and
// parameter map onto `from_parameters`.
let components = ComponentRegistry::new().with(RunningMax::KIND, |spec| {
    RunningMax::from_parameters(
        spec.name.as_str(),
        spec.require("in")?,
        spec.require("reset")?,
        spec.require("out")?,
        spec.parameters,
    )
    .map(|component| Box::new(component) as Box<dyn Component>)
    .map_err(BuildError::other)
});

// A model document instantiating the kind on a local `sim` device.
let model = PlantModel::load(
    r#"{
      "version": 1,
      "devices": [{
        "id": 1, "kind": "sim",
        "channels": {
          "level": {"direction": "in", "value_type": "float"},
          "reset": {"direction": "in", "value_type": "bool"},
          "peak":  {"direction": "out", "value_type": "float"}
        }
      }],
      "io_points": [
        {"id": 1, "direction": "in",  "value_type": "float",
         "channel": {"device": 1, "name": "level"}},
        {"id": 2, "direction": "in",  "value_type": "bool",
         "channel": {"device": 1, "name": "reset"}},
        {"id": 3, "direction": "out", "value_type": "float",
         "channel": {"device": 1, "name": "peak"}}
      ],
      "signals": [{"id": 1, "name": "level-peak", "source": 3}],
      "components": [{
        "id": 1, "kind": "running-max",
        "parameters": {"initial": {"float": 0.0}},
        "ports": {
          "in":    {"direction": "in",  "value_type": "float"},
          "reset": {"direction": "in",  "value_type": "bool"},
          "out":   {"direction": "out", "value_type": "float"}
        }
      }],
      "connections": [
        {"from": {"point": 1}, "to": {"port": {"component": 1, "name": "in"}}},
        {"from": {"point": 2}, "to": {"port": {"component": 1, "name": "reset"}}},
        {"from": {"port": {"component": 1, "name": "out"}}, "to": {"point": 3}}
      ]
    }"#,
)
.unwrap();
let driver = resolve_drivers(&model, &DriverRegistry::standard())
    .unwrap()
    .build()
    .unwrap();
let mut executor = assemble(&model, &components, &driver).unwrap();

// The plant side drives the `in` point; each scan publishes the peak.
driver.write(PointId(1), Value::Float(5.0)).unwrap();
executor.scan();
assert_eq!(driver.read(PointId(3)).unwrap().value, Value::Float(5.0));

driver.write(PointId(1), Value::Float(3.0)).unwrap();
executor.scan();
assert_eq!(driver.read(PointId(3)).unwrap().value, Value::Float(5.0));

driver.write(PointId(2), Value::Bool(true)).unwrap();
executor.scan();
assert_eq!(driver.read(PointId(3)).unwrap().value, Value::Float(3.0));

// The snapshot carries the instance's descriptor and diagnostics.
assert_eq!(executor.snapshot().descriptors[0].kind, "running-max");
assert_eq!(executor.snapshot().components[0].step_errors, 0);
```

## Declared commands and emitted events

Steps 2 and 4 cover the adapted surface: ports become measurement and
state resources, parameters become configuration, and the generic
`write_value`/`force_point`/`unforce_point`/`set_parameter` variants
adapt into the interface's `commands` entries. A kind may additionally
declare *native* commands and events — the `Declared`-provenance
vocabulary decision 82 adds — when a writable point or tunable parameter
cannot express the behavior: a one-shot action, an action with typed
arguments, an action whose availability the kind itself decides, or a
typed event the block emits rather than a point transition the monitor
observes. The checked-in example is `dcs_blocks::Sequencer`
(`crates/dcs-blocks/src/sequencer.rs`): it declares the `advance`/`reset`
commands and the `step_completed` event beside its `run`/`reset` level
inputs — deliberately not writable-point aliases, because `reset` the
command is a one-shot where the `reset` input is a held condition.

### Declaring a native command

`describe()`'s returned `ComponentDescriptor.commands` lists
`CommandDecl`s:

- `name` — the command's stable identity within the interface, unique
  across the derived `commands` collection (so it may not collide with
  an adapted `write_value:<port>`/`set_parameter:<param>` name);
- `request` — the typed argument schema, `CommandArgument`s of
  `name` plus `ValueKind`;
- `availability` — `CommandAvailability::Always` (submittable whenever
  the instance exists) or `KindDeclared` (the kind's own predicate
  decides per submission and reports the refusal reason).

A consumer submits one as `Command::Invoke { component, command,
arguments }` — `component` the instance name, `command` the declared
name, `arguments` keyed by the declared argument names. Submission-time
validation refuses an unknown component, an undeclared command name, or
a declared argument carrying the wrong `Value` kind before the command
ever queues (`UnknownComponent`, `UnknownCommand`,
`ArgumentTypeMismatch` — each naming the instance); what the schema does
not constrain — a supplied argument name the schema does not declare, a
missing argument, a value outside the command's domain, the
`KindDeclared` predicate — is the implementation's to refuse in
`Component::invoke_command`, which the executor calls at the scan
boundary in deterministic submission order.
`Ok` applies the command; `Err(reason)` settles the invocation
`command_refused` carrying the reason verbatim. Either way the ordinary
receipted path answers: one submission, one `CommandReceipt`, one
journaled `command_settled` — an invoke is never fire-and-forget and
never an alias of a writable point.

```rust,ignore
fn invoke_command(
    &mut self,
    command: &str,
    arguments: &BTreeMap<String, Value>,
) -> Result<(), String> {
    match command {
        "advance" => { /* validate `count`, mutate run state, Ok(()) */ }
        _ => unreachable!("submission validates the declared command name"),
    }
}
```

The default hook refuses every invocation, so a kind declaring commands
it does not serve still settles `command_refused` rather than silently
succeeding. **Checkpoint obligation:** command-mutated state is run
state — fold every field the command touches into `capture_state`, or a
tracking standby will not inherit the effect (decision 84).

A kind declaring a `kind_declared`-availability command also implements
`Component::command_refusal`, the standing-availability probe the
executor evaluates once per declared `kind_declared` command at each
scan's step end and publishes on the snapshot's `command_verdicts`
section: `None` reports the command invocable now, `Some(reason)` the
kind's standing refusal — the same text a refused invocation settles.
The probe is argument-free: it answers whether the command is invocable
at all now, so argument-domain refusals stay in `invoke_command`.
Factor the standing predicate once and have dispatch consult it — the
published verdict and the refusal must be the same expression. The
verdict is advisory only: submissions still validate, queue, and settle
through the receipted path, and a verdict dispatch disagrees with
settles honestly rather than failing the scan. The default reports
every declared command invocable — the unconditional `available` a
publication carrying no verdict still serves.

### Declaring an emitted event

`ComponentDescriptor.events` lists `EventDecl`s: `name` (the stable
event-kind identity), `payload` (`EventField`s of `name`,
`EventFieldKind` — a `Value` of a kind, a `Quality`, a `Receipt`, or
free `Text` — plus an `optional` flag for fields that may carry no
value), and `retention` (`EventRetention::Journal` for the durable
transition record; `History`/`Latest` are declared in the vocabulary but
route to no consumer-visible store yet — treat them as reserved). The
component emits by pushing `EmittedEvent`s (`event` naming the
declaration, `fields` keyed by the declared field names) into a buffer
`Component::drain_events` empties; the executor drains after every
`step`, success or failure, stamps each event's `component` with the
registered instance name, and journal-retained emissions land as
`JournalEvent::EventEmitted` at the producing scan's tick. An emission
the descriptor never declares still journals — the audit record never
drops an event — but the drift test treats declaration as the contract.

**Checkpoint obligation for emitting kinds:** an emitted event's
sequence is derived state. A kind that emits sequence-bearing events
must checkpoint everything feeding the sequence — decision 84's
emit-identical rule makes a tracking standby re-derive the same
emissions, so a kind emitting from non-checkpointed state would break a
promoted run's indistinguishable journal. `Sequencer` checkpoints the
step position its `step_completed` emissions report.

### The spec mirror and the generic consumer

A kind declaring commands or events mirrors them on its `dcs-build`
spec — `declared_commands()`/`declared_events()` returning the same
`CommandDecl`/`EventDecl` data (`crates/dcs-build/src/specs.rs`, the
`sequencer` spec is the worked example) — so the engineering
composition, the runtime descriptor, and the served schema share one
declaration, pinned by the spec-drift sweep.

A generic consumer needs no kind-specific code:

- `GET /schema` serves the instance-level `BlockInterface` per
  component — declared commands and events appear under `Declared`
  provenance beside the adapted entries;
- `GET /resources` joins the live half — per-command `available` or
  the named refusal the submission path would answer, and the retained
  journal tail's entries attributed to the instance, `event_emitted`
  records included;
- the monitoring page renders the command table with typed argument
  controls and the recent-events list from those two documents alone;
- `dcs-ctl invoke <component> <command> [<name>=<value>]...` submits a
  declared command through the same receipted path without a browser.

A `KindDeclared` command's served `available` joins the published
verdict: while the kind's predicate refuses, the resource view reports
`available: false` carrying the kind's named refusal reason. The served
answer is advisory — it is the last completed scan's standing verdict,
so a submission still validates, queues, and settles through the
receipted path, and a refusal the verdict predates (an argument-domain
check, an `invoke_command` invariant) still lands on the settled
`command_refused` receipt for consumers to surface.

## Adding a device kind

The checked-in references are the three factories in
`crates/dcs-assembly/src/drivers.rs` that `DriverRegistry::standard()`
installs: `sim_device` serving the `sim*` prefix by contributing to the shared
local simulated map, `sim_tcp_device` serving the exact `SIM_TCP_KIND`
(`"sim-tcp"`) by connecting a `dcs_sim_net::RemoteDriver` over TCP, and
`scripted_device` serving the exact `SIM_SCRIPTED_KIND` (`"sim-scripted"`) by
building a `dcs_sim::ScriptedDriver` that replays a tick-indexed `"script"`
parameter — the checked-in `Backend` contribution that also installs an
`inspect` handle.

### 1. Implement `IoDriver`

`dcs_core::IoDriver` (`crates/dcs-core/src/io.rs`) is the untyped
point-facing contract:

- `read(point) -> Result<Sample, IoError>` — the most recent sample, or
  `IoError::UnknownPoint` for a point the driver does not serve.
- `write(point, value) -> Result<(), IoError>` — reject a value whose kind
  differs from the point's declared kind with `IoError::TypeMismatch`.
- `IoError::Disconnected` / `IoError::Timeout` report communication failure;
  every variant carries the offending `PointId`.

The trait is object-safe and takes `&self`, so drivers use interior
mutability, and a `DeviceBackend` requires `Arc<dyn IoDriver + Send + Sync>`.

Two optional hooks decide which standby-observation kind the driver is
(decision 13): `capture_state()`/`restore_state()` for drivers holding
transferable state (`SimDriver` captures the simulated field), or the `None`
default for field-observing drivers whose state is the plant itself
(`RemoteDriver`). `FanoutDriver` merges captured backend state under a
`"{index}.{field}"` namespace.

### 2. Write the factory

A device-kind factory has the shape `Fn(&DeviceSpec<'_>) ->
Result<DeviceDriver, DeviceError>`. The `DeviceSpec` hands the factory
everything the model declared for the device:

- `spec.id`, `spec.kind` — the device id and, under a prefix registration,
  the device's actual kind string;
- `spec.hardware` — the model's `hardware` marker: `true` declares the
  device hardware-bound. A hardware-bound kind's factory requires it; a
  simulated kind's factory rejects it — the marker is honest evidence in
  both directions, never a hint a backend may ignore;
- `spec.parameters` — kind-specific addressing as arbitrary JSON
  (`BTreeMap<String, serde_json::Value>`); the model treats it as opaque, so
  the factory owns all validation;
- `spec.channels` — the declared channel names with their directions and
  value kinds;
- `spec.points` — the `DevicePoint`s (`point`, `channel`, `direction`,
  `kind`) of every `io_point` bound to the device's channels, already
  guaranteed by model validation to agree with the channels. The returned
  backend must serve exactly these points.

Failures map to assembly errors naming the device and kind:

- `DeviceError::parameters(detail)` → `AssemblyError::InvalidDeviceParameters`
  for missing, mistyped, or unknown parameters;
- `DeviceError::backend(detail)` → `AssemblyError::DeviceBackend` for a
  backend that cannot be built or cannot serve the declared points.

Validate parameters first, then build — and probe eagerly where the backend
allows it. The `sim-tcp` factory is the pattern: it rejects unknown keys,
requires `address` as a resolvable `host:port` string, accepts an optional
non-negative integral `timeout_ms`, connects immediately (an unreachable
endpoint is `DeviceError::backend`, not a mid-scan surprise), and reads every
declared point once to verify the remote plant serves it with the declared
value kind.

### 3. Choose the contribution

A factory returns one of two `DeviceDriver` contributions:

- `DeviceDriver::Sim(ChannelMap)` — the device's points join the shared
  local simulated map as `PointBinding`s (channel identity plus a neutral
  initial value). The fragment merges with every other `Sim` contribution
  and the synthesized internal points into one `SimDriver` backend, so a
  model can mix many `sim*` devices freely.
- `DeviceDriver::Backend(DeviceBackend { io, step, claim, release, inspect, field_facing })` — a
  self-contained backend. `io` is the point-facing driver; `step` is an
  optional `StepHook` (`Fn(f64) -> Result<Tick, dcs_assembly::StepError>`)
  advancing the backend's simulated plant one `dt` per `FanoutDriver::step` —
  remote or real field kinds that advance themselves leave it `None`.
  (`RemoteDriver` supplies one because the remote plant is stepped explicitly
  over its protocol.) `claim` is an optional `ClaimHook`
  (`Fn(u64) -> Result<(), dcs_assembly::StepError>`) taking the field's
  write-ownership for an owner token — the single-writer arbitration a
  promoted standby runs before its gate lifts: `sim-tcp` installs the
  plant server's claim, and a field-facing kind that cannot arbitrate
  leaves it `None`, which keeps automatic failover off for models built
  on it (`FanoutDriver::unfenced_field_devices` names such devices).
  `release` is an optional `ReleaseHook` (`Fn()`) — the demotion
  counterpart of `claim`: `FanoutDriver::release_field_claims` runs it
  when the peer gives up ownership so the backend forgets any recorded
  claim token it would otherwise re-assert on a reconnect. `sim-tcp`
  installs `RemoteDriver::release_claim` for exactly that — a demoted
  attachment must not race the new owner back onto a restarted plant.
  `inspect` is an optional
  `Option<Arc<dyn Any + Send + Sync>>` typed handle the factory installs when
  the backend exposes more than the `IoDriver` surface — `sim-scripted`
  installs the `ScriptedDriver` itself so `FanoutDriver::inspect::<T>(device)`
  reaches its recorded-write log; leave it `None` when the backend has
  nothing to inspect. `field_facing` tells a redundant pair whether the
  backend reaches the shared field: `true` for remote or real field kinds
  (`sim-tcp`), `false` for backends private to the instance — a tracking
  standby's write gate quiesces only field-facing points, and
  `FanoutDriver::step_local` leaves a field-facing backend's plant clock
  to the peer owning the field.

Point-to-point connections whose ends live on different backends become
`FanoutDriver` routes applied at each `step`; both-sim wires stay inside the
local `SimDriver` as `Loopback`s, and wires between internal points become
links inside the executor's scan image.

### 4. Register the kind

`DriverRegistry::with`/`register` bind an exact kind string;
`with_prefix`/`register_prefix` bind every kind starting with a prefix, for
families like `sim` whose members (`sim-ai`, `sim-ao`, …) share one
integration. Exact registrations always win over prefix matches — that is how
`sim-tcp` and `sim-scripted` route to their own backends while every other
`sim*` kind stays local. `DriverRegistry::standard()` installs the built-in
three; a deployment adds its integrations onto it (or starts from
`DriverRegistry::new()`), then `resolve_drivers(&model, &registry)` produces
the `DriverPlan` whose `build()` yields the `FanoutDriver`. An unregistered
kind fails as `AssemblyError::UnknownDeviceKind` before any backend is built.

### 5. Declare the device in a model

A `devices` entry declares `id`, `kind`, `channels` (each named channel's
`direction` and `value_type`), and optional `parameters`; `io_points` bind
logical points to `{device, name}` channel references. Model validation
requires the channel to exist and to agree with the point's direction and
value type. An `io_point` may instead omit `channel` and carry an `initial`
value — a *channel-less internal point* held by the controller's scan image
rather than field I/O; internal points reach no device factory, so they never
appear in a `DeviceSpec`. The checked-in
`crates/dcs-assembly/fixtures/mixed_kinds.json` shows a model splitting one
loop across a local `sim` device and a remote `sim-tcp` device carrying
`{"address": "…"}` parameters, `scripted_kinds.json` declares a
`sim-scripted` device, and `internal_points.json` declares internal points;
their tests in `crates/dcs-assembly/tests/` exercise the whole registry path.

### Worked example: a new device kind, end to end

This example registers a `memory` device kind — an in-memory backend serving
its declared points, with one optional `initial` parameter mapping channel
names to start values — then runs a model that drives an `analog-input`
component through it. It compiles and runs as part of the test suite.

```rust
use dcs_assembly::{
    assemble, resolve_drivers, AssemblyError, BuildError, ComponentRegistry, DeviceBackend,
    DeviceDriver, DeviceError, DeviceSpec, DriverRegistry,
};
use dcs_blocks::AnalogInput;
use dcs_core::{IoDriver, IoError, PointId, Sample, Tick, Value, ValueKind};
use dcs_model::PlantModel;
use dcs_runtime::Component;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

/// The backend: `In` points hold values the plant side sets through
/// `write`; `Out` points record controller writes.
struct MemoryDriver {
    samples: Mutex<HashMap<PointId, Sample>>,
    kinds: HashMap<PointId, ValueKind>,
}

impl IoDriver for MemoryDriver {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        self.samples
            .lock()
            .unwrap()
            .get(&point)
            .copied()
            .ok_or(IoError::UnknownPoint(point))
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        match self.kinds.get(&point) {
            None => Err(IoError::UnknownPoint(point)),
            Some(&expected) if expected != value.kind() => {
                Err(IoError::TypeMismatch {
                    point,
                    expected,
                    found: value,
                })
            }
            Some(_) => {
                self.samples
                    .lock()
                    .unwrap()
                    .insert(point, Sample::good(value, Tick::ZERO));
                Ok(())
            }
        }
    }
}

/// Converts a JSON `initial` entry into a `Value`, rejecting anything but
/// booleans and numbers.
fn initial_value(channel: &str, json: &serde_json::Value) -> Result<Value, DeviceError> {
    match json {
        serde_json::Value::Bool(b) => Ok(Value::Bool(*b)),
        serde_json::Value::Number(number) => {
            if let Some(int) = number.as_i64() {
                Ok(Value::Int(int))
            } else {
                number.as_f64().map(Value::Float).ok_or_else(|| {
                    DeviceError::parameters(format!(
                        "\"initial\" for channel {channel:?} is not representable"
                    ))
                })
            }
        }
        other => Err(DeviceError::parameters(format!(
            "\"initial\" for channel {channel:?} must be a bool or number, found {other}"
        ))),
    }
}

fn neutral(kind: ValueKind) -> Value {
    match kind {
        ValueKind::Bool => Value::Bool(false),
        ValueKind::Int => Value::Int(0),
        ValueKind::Float => Value::Float(0.0),
    }
}

/// The `"memory"` kind's factory. Parameter validation first: `initial`
/// is optional and object-shaped, every other key is rejected, and each
/// entry must match its channel's declared value kind.
fn memory_device(spec: &DeviceSpec<'_>) -> Result<DeviceDriver, DeviceError> {
    for name in spec.parameters.keys() {
        if name != "initial" {
            return Err(DeviceError::parameters(format!("unknown parameter {name:?}")));
        }
    }
    let initials = match spec.parameters.get("initial") {
        None => None,
        Some(value) => Some(value.as_object().ok_or_else(|| {
            DeviceError::parameters(
                "\"initial\" must be an object mapping channel names to values",
            )
        })?),
    };
    let mut samples = HashMap::new();
    let mut kinds = HashMap::new();
    for point in &spec.points {
        let initial = match initials.and_then(|map| map.get(point.channel.as_str())) {
            None => neutral(point.kind),
            Some(json) => {
                let value = initial_value(&point.channel, json)?;
                if value.kind() != point.kind {
                    return Err(DeviceError::parameters(format!(
                        "\"initial\" for channel {:?} is {:?}, the channel declares {:?}",
                        point.channel,
                        value.kind(),
                        point.kind
                    )));
                }
                value
            }
        };
        kinds.insert(point.point, point.kind);
        samples.insert(point.point, Sample::good(initial, Tick::ZERO));
    }
    // A self-contained backend; nothing to step — the device holds no
    // simulated dynamics — and nothing beyond the IoDriver surface to
    // inspect. It is private to the instance, not field-facing, so no
    // write-ownership claim applies.
    Ok(DeviceDriver::Backend(DeviceBackend {
        io: Arc::new(MemoryDriver {
            samples: Mutex::new(samples),
            kinds,
        }),
        step: None,
        claim: None,
        release: None,
        inspect: None,
        field_facing: false,
    }))
}

let model = PlantModel::load(
    r#"{
      "version": 1,
      "devices": [{
        "id": 1, "kind": "memory",
        "parameters": {"initial": {"raw": 5.0}},
        "channels": {
          "raw": {"direction": "in",  "value_type": "float"},
          "eng": {"direction": "out", "value_type": "float"}
        }
      }],
      "io_points": [
        {"id": 1, "direction": "in",  "value_type": "float",
         "channel": {"device": 1, "name": "raw"}},
        {"id": 2, "direction": "out", "value_type": "float",
         "channel": {"device": 1, "name": "eng"}}
      ],
      "signals": [{"id": 1, "name": "level-eng", "source": 2}],
      "components": [{
        "id": 1, "kind": "analog-input",
        "parameters": {
          "raw_min": {"float": 0.0}, "raw_max": {"float": 10.0},
          "eng_min": {"float": 0.0}, "eng_max": {"float": 100.0}
        },
        "ports": {
          "raw": {"direction": "in",  "value_type": "float"},
          "out": {"direction": "out", "value_type": "float"}
        }
      }],
      "connections": [
        {"from": {"point": 1}, "to": {"port": {"component": 1, "name": "raw"}}},
        {"from": {"port": {"component": 1, "name": "out"}}, "to": {"point": 2}}
      ]
    }"#,
)
.unwrap();

// Without the registration the kind is unknown; with it the model builds.
assert!(matches!(
    resolve_drivers(&model, &DriverRegistry::standard()),
    Err(AssemblyError::UnknownDeviceKind { .. })
));
let drivers = DriverRegistry::standard().with("memory", memory_device);
let driver = resolve_drivers(&model, &drivers).unwrap().build().unwrap();

let components = ComponentRegistry::new().with(AnalogInput::<f64>::KIND, |spec| {
    AnalogInput::<f64>::from_parameters(
        spec.name.as_str(),
        spec.require("raw")?,
        spec.require("out")?,
        spec.parameters,
    )
    .map(|component| Box::new(component) as Box<dyn Component>)
    .map_err(BuildError::other)
});
let mut executor = assemble(&model, &components, &driver).unwrap();

// `raw` starts at its declared initial 5.0 -> the first scan writes 50.0.
executor.scan();
assert_eq!(driver.read(PointId(2)).unwrap().value, Value::Float(50.0));

// The plant side moves the input; the next scan follows.
driver.write(PointId(1), Value::Float(10.0)).unwrap();
executor.scan();
assert_eq!(driver.read(PointId(2)).unwrap().value, Value::Float(100.0));
```

### Cyclic device kinds: the `CyclicIoDriver` contract

Everything above is the per-point contract: `read`/`write` answer one
point per call, and each call may transport. Some field transports do
not have that shape — a fieldbus like EtherCAT exchanges the whole
process image once per cycle, inputs arriving atomically in one frame
while the staged outputs publish in the same frame. Decision 78
(`docs/architecture.md`) adds the opt-in seam for that shape:
`IoDriver::cyclic`, defaulting to `None`, answers
`Some(&dyn CyclicIoDriver + Sync)` on a driver implementing
`CyclicIoDriver::exchange(tick)` — the one call allowed to touch the
transport.

The checked-in consumer is the `ethercat` kind (`crates/dcs-ethercat`):
a `BusMaster` owns the staged output image, the latched input image,
the miss accounting, and the exchange diagnostics of one logical bus
over a `BusTransport`; each `EthercatDevice` is the per-device
`IoDriver` surface, and only the bus's first attacher carries
`cyclic()` and `diagnostics`, so a shared bus exchanges and counts once
per scan. `EthercatBuses::with_opener` driving a scripted
`testing::FakeTransport` is the no-hardware path a cyclic
integration's contract tests take, and the scripted `CyclicStub` in
`crates/dcs-runtime`'s executor tests pins every element below.

#### Opting in — and not

A kind opts in only when its transport genuinely turns a process image
once per scan: the driver then keeps two local images — the *input
image* the last completed exchange latched and the *output image*
`write` stages — and its `read`/`write` become image-local, never
transporting. The extension trait makes support and implementation
atomic: `cyclic()` can answer `Some` only where an `exchange` impl
exists.

A kind must *not* opt in when:

- its transport answers per point — the shipped `sim*`, `sim-tcp`,
  `sim-scripted`, and `sim-bus` kinds all keep the default `None`.
  There is no once-per-scan frame for `exchange` to run; faking the
  image would only hide per-point transport behind a no-op boundary and
  blur the failure semantics the contract exists to make precise — one
  counted boundary failure, not a fault per covered point;
- it cannot hold the image semantics below — a `read` that still needs
  the wire, an input image it cannot latch atomically, or no
  defensible `exchange_miss_threshold` to declare as device data;
- its transport has no exchange boundary at all — an event-driven or
  polled-snapshot device whose freshness is per point, not per frame.

#### The exchange boundary

The executor detects the surface at wiring — `IoDriver::cyclic` on the
assembled driver — and calls `exchange(tick)` exactly once per scan,
ordered *after* queued commands apply (so a command-staged write
publishes in the same exchange) and *before* the per-point input reads
that serve the image it latched. The scan's write phase runs later
still, so a value a component writes in scan `t` stages into the output
image and publishes in scan `t + 1`'s exchange — the contract's
documented one-scan actuation delay.

A failed `exchange` completes nothing — the input image holds its
previous latch and the staged output image is retained for the next
exchange — and counts once at the boundary under
`IoHealth.failed_exchanges`, `consecutive_failures`, and `last_error`
(attributed `In`, the boundary it opens). The scan never aborts on it:
the held image still answers the reads that follow.

#### Image semantics, aging, and escalation

One exchange publishes the output image staged since the previous
exchange and latches the returned input image atomically, stamping each
latched sample with the exchange's `tick` as its acquisition stamp.
That stamp is what a point's declared `stale_after_ticks` budget
measures (see `docs/model-reference.md`): while exchanges keep landing,
samples are as fresh as the scan; while they miss, the held value ages
to `Uncertain(Stale)` past its budget — designed degradation, not
failure.

While consecutive misses stay under the device's declared
`exchange_miss_threshold` parameter — the contract-settled name; a
cyclic kind takes it as device-parameter data exactly like `sim-bus`'s
register addressing — `read` keeps serving the held image. At or past
the threshold, reads escalate to `IoError::Disconnected`: an ordinary
per-point boundary fault from then on, counted under `failed_reads`
with the point's held value degrading to `Bad`. `write` staging never
escalates — staged outputs publish on the next exchange that
completes.

An exchange that completes short — a working-counter shortfall naming
one station — latches the answering stations' data and escalates only
the named station's points; an unattributable failure degrades the
whole bus. Completed-but-short exchanges count under
`working_counter_mismatches`, not `failed_exchanges`.

#### Diagnostics

A cyclic driver's `diagnostics()` reports the link `Disconnected` while
any miss stands and carries `exchange: Some(ExchangeDiagnostics)` —
the `attempted`/`succeeded` counters, `working_counter_mismatches`,
`last_exchange_tick` (the acquisition stamp the held image carries),
and `missed_deadlines` for completed-but-late exchanges. These are
bus-level counters, deliberately distinct from the executor's
per-point ones; the section is serde-optional, so payloads serialized
before the contract existed stay readable.

#### Gate and fan-out forwarding

Wrappers forward or aggregate the surface exactly as they do for
`diagnostics`:

- `dcs_runtime::WriteGate` passes `cyclic()` through — the exchange is
  not a write, so a quiesced standby still latches fresh inputs while
  the writes its gate dropped never reached the driver's staged image:
  nothing the standby computed publishes;
- `FanoutDriver` answers `Some` when any backend is cyclic; its
  `exchange` turns each cyclic backend's image in backend order, the
  first failing backend ending the call — a bus the call never reached
  simply holds its image for the next scan. Its `diagnostics` merges
  each reporting backend's `exchange` section — counters sum over the
  buses and `last_exchange_tick` takes the earliest reported, the
  freshest exchange every bus completed — so a shared-bus kind must
  report its counters once, as `ethercat`'s owner-only surface does.

#### Worked example: a cyclic device kind, end to end

This example registers a `demo-bus` kind — a two-image backend whose
`field` map stands in for the frame a real `exchange` moves — then runs
a model through it: one exchange per scan, the held image's aging and
miss-threshold escalation, the one-scan actuation delay, the exchange
diagnostics, and the write gate's forwarding. It compiles and runs as
part of the test suite; the shipped reference is `dcs-ethercat`'s
`EthercatDevice`/`BusMaster`, which holds the same contract over a real
`BusTransport`.

```rust
use dcs_assembly::{
    assemble, resolve_drivers, ComponentRegistry, DeviceBackend, DeviceDriver, DeviceError,
    DeviceSpec, DriverRegistry,
};
use dcs_core::{
    CyclicIoDriver, Direction, DriverDiagnostics, ExchangeDiagnostics, IoDriver, IoError,
    LinkState, PointId, Quality, QualityReason, Sample, Tick, Value, ValueKind,
};
use dcs_model::{DeviceId, PlantModel};
use std::any::Any;
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

/// A cyclic backend: `exchange` is the only call that touches the
/// field — the `field` map stands in for the frame a real transport
/// moves. `read` serves the input image the last completed exchange
/// latched and `write` stages the pending output image; neither
/// transports.
struct CyclicBus {
    /// Every bound point's direction and declared value kind.
    points: HashMap<PointId, (Direction, ValueKind)>,
    /// The device-declared `exchange_miss_threshold`: consecutive
    /// uncompleted exchanges before reads escalate to `Disconnected`.
    miss_threshold: u64,
    state: Mutex<BusImages>,
    /// Instrumentation: the transport-call count — `exchange` is the
    /// only increment — and the outage flag a test asserts against.
    exchanges: AtomicU64,
    link_down: AtomicBool,
}

/// The images and accounting behind the backend's mutex.
struct BusImages {
    /// The far side — what the exchange moves data to and from.
    field: HashMap<PointId, Value>,
    /// The held input image the last completed exchange latched.
    latched: HashMap<PointId, Sample>,
    /// The pending output image `write` stages — retained across a
    /// failed exchange, published by the next completed one.
    staged: HashMap<PointId, Value>,
    /// Consecutive uncompleted exchanges — the miss count the declared
    /// `exchange_miss_threshold` compares against.
    misses: u64,
    /// The exchange counters `diagnostics` reports.
    attempted: u64,
    succeeded: u64,
    last_exchange_tick: Option<Tick>,
    last_error: Option<String>,
}

impl CyclicBus {
    /// Plants `value` on the field — what the far side asserts between
    /// exchanges. The held image sees it only once an exchange latches.
    fn field_set(&self, point: PointId, value: Value) {
        self.state.lock().unwrap().field.insert(point, value);
    }

    /// What the field currently carries — the published output side.
    fn field_value(&self, point: PointId) -> Option<Value> {
        self.state.lock().unwrap().field.get(&point).copied()
    }

    /// Scripts a transport outage: while down, every exchange fails.
    fn set_link_down(&self, down: bool) {
        self.link_down.store(down, Ordering::Relaxed);
    }

    /// The exchange count — the transport calls per-point access never
    /// makes.
    fn exchange_count(&self) -> u64 {
        self.exchanges.load(Ordering::Relaxed)
    }
}

impl IoDriver for CyclicBus {
    /// Serves the held input image — never the field. At or past the
    /// declared miss threshold, or for a point the driver does not
    /// serve, the read fails. An output read serves the pending staged
    /// image: what the next completed exchange will publish.
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        let (direction, _) = *self
            .points
            .get(&point)
            .ok_or(IoError::UnknownPoint(point))?;
        let state = self.state.lock().unwrap();
        if state.misses >= self.miss_threshold {
            return Err(IoError::Disconnected(point));
        }
        match direction {
            Direction::In => state
                .latched
                .get(&point)
                .copied()
                .ok_or(IoError::UnknownPoint(point)),
            Direction::Out => state
                .staged
                .get(&point)
                .map(|&value| Sample::good(value, state.last_exchange_tick.unwrap_or(Tick::ZERO)))
                .ok_or(IoError::UnknownPoint(point)),
        }
    }

    /// Stages the pending output image — never the field. The
    /// `UnknownPoint`/`TypeMismatch` semantics are the per-point
    /// contract's, unchanged; the field owns the input image, so an
    /// `in` point has no writable surface.
    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        let (direction, kind) = *self
            .points
            .get(&point)
            .ok_or(IoError::UnknownPoint(point))?;
        if direction != Direction::Out {
            return Err(IoError::UnknownPoint(point));
        }
        if value.kind() != kind {
            return Err(IoError::TypeMismatch {
                point,
                expected: kind,
                found: value,
            });
        }
        self.state.lock().unwrap().staged.insert(point, value);
        Ok(())
    }

    /// The cyclic half of the I/O-health surface: the exchange counters
    /// and the link state — `Disconnected` while any miss stands.
    fn diagnostics(&self) -> Option<DriverDiagnostics> {
        let state = self.state.lock().unwrap();
        Some(DriverDiagnostics {
            link: if state.misses > 0 {
                LinkState::Disconnected
            } else {
                LinkState::Connected
            },
            last_error: state.last_error.clone(),
            exchange: Some(ExchangeDiagnostics {
                attempted: state.attempted,
                succeeded: state.succeeded,
                working_counter_mismatches: 0,
                last_exchange_tick: state.last_exchange_tick,
                missed_deadlines: 0,
            }),
        })
    }

    /// `Some` opts the backend into the cyclic contract — the executor
    /// detects the surface at wiring and calls `exchange` once per scan.
    fn cyclic(&self) -> Option<&(dyn CyclicIoDriver + Sync)> {
        Some(self)
    }
}

impl CyclicIoDriver for CyclicBus {
    /// One process-image exchange for `tick`: publish the staged output
    /// image, then latch the returned input image atomically, stamping
    /// each sample with `tick` — the acquisition stamp a
    /// `stale_after_ticks` budget measures.
    fn exchange(&self, tick: Tick) -> Result<(), IoError> {
        self.exchanges.fetch_add(1, Ordering::Relaxed);
        let mut guard = self.state.lock().unwrap();
        let state = &mut *guard;
        state.attempted += 1;
        if self.link_down.load(Ordering::Relaxed) {
            // A failed exchange completes nothing: the input image
            // holds, the staged output image is retained for the next
            // exchange, and the miss counts once at the boundary.
            state.misses += 1;
            state.last_error = Some("the exchange did not complete".to_string());
            return Err(IoError::Disconnected(
                *self.points.keys().min().expect("a nonempty image"),
            ));
        }
        for (&point, &value) in &state.staged {
            state.field.insert(point, value);
        }
        let latched: Vec<(PointId, Sample)> = state
            .field
            .iter()
            .filter(|(point, _)| self.points[point].0 == Direction::In)
            .map(|(&point, &value)| (point, Sample::good(value, tick)))
            .collect();
        for (point, sample) in latched {
            state.latched.insert(point, sample);
        }
        state.misses = 0;
        state.succeeded += 1;
        state.last_exchange_tick = Some(tick);
        Ok(())
    }
}

fn neutral(kind: ValueKind) -> Value {
    match kind {
        ValueKind::Bool => Value::Bool(false),
        ValueKind::Int => Value::Int(0),
        ValueKind::Float => Value::Float(0.0),
    }
}

/// The `"demo-bus"` kind's factory. `exchange_miss_threshold` is the
/// contract-settled parameter name a cyclic kind declares — required,
/// a positive integer.
fn demo_bus(spec: &DeviceSpec<'_>) -> Result<DeviceDriver, DeviceError> {
    for name in spec.parameters.keys() {
        if name != "exchange_miss_threshold" {
            return Err(DeviceError::parameters(format!("unknown parameter {name:?}")));
        }
    }
    let miss_threshold = spec
        .parameters
        .get("exchange_miss_threshold")
        .and_then(serde_json::Value::as_u64)
        .filter(|&n| n >= 1)
        .ok_or_else(|| {
            DeviceError::parameters("\"exchange_miss_threshold\" must be a positive integer")
        })?;
    let mut points = HashMap::new();
    let mut field = HashMap::new();
    let mut latched = HashMap::new();
    let mut staged = HashMap::new();
    for point in &spec.points {
        points.insert(point.point, (point.direction, point.kind));
        match point.direction {
            // The field asserts inputs; its seed doubles as the
            // pre-first-exchange held image.
            Direction::In => {
                field.insert(point.point, neutral(point.kind));
                latched.insert(point.point, Sample::good(neutral(point.kind), Tick::ZERO));
            }
            // The pending output image seeds with the safe state a
            // field kind stages before the first exchange.
            Direction::Out => {
                staged.insert(point.point, neutral(point.kind));
            }
        }
    }
    let bus = Arc::new(CyclicBus {
        points,
        miss_threshold,
        state: Mutex::new(BusImages {
            field,
            latched,
            staged,
            misses: 0,
            attempted: 0,
            succeeded: 0,
            last_exchange_tick: None,
            last_error: None,
        }),
        exchanges: AtomicU64::new(0),
        link_down: AtomicBool::new(false),
    });
    // A field-facing backend — the kind reaches the shared field — with
    // nothing to step (a field transport advances itself) and no
    // write-ownership claim to arbitrate. `inspect` carries the backend
    // so tooling — and this example — can reach its images.
    let inspect: Arc<dyn Any + Send + Sync> = bus.clone();
    Ok(DeviceDriver::Backend(DeviceBackend {
        io: bus,
        step: None,
        claim: None,
        release: None,
        inspect: Some(inspect),
        field_facing: true,
    }))
}

let model = PlantModel::load(
    r#"{
      "version": 1,
      "devices": [{
        "id": 1, "kind": "demo-bus",
        "parameters": {"exchange_miss_threshold": 3},
        "channels": {
          "level": {"direction": "in",  "value_type": "float"},
          "valve": {"direction": "out", "value_type": "float"}
        }
      }],
      "io_points": [
        {"id": 1, "direction": "in",  "value_type": "float",
         "stale_after_ticks": 1,
         "channel": {"device": 1, "name": "level"}},
        {"id": 2, "direction": "out", "value_type": "float",
         "channel": {"device": 1, "name": "valve"}}
      ],
      "signals": [{"id": 1, "name": "level", "source": 1}],
      "components": [],
      "connections": []
    }"#,
)
.unwrap();

let drivers = DriverRegistry::standard().with("demo-bus", demo_bus);
let driver = resolve_drivers(&model, &drivers).unwrap().build().unwrap();
// The fan-out aggregated the cyclic surface: the backend opted in, so
// the assembled driver answers `Some` and every scan will exchange.
assert!(driver.cyclic().is_some());
// The factory's `inspect` handle reaches the backend behind the fan-out.
let bus = driver.inspect::<CyclicBus>(DeviceId(1)).unwrap();
let mut executor = assemble(&model, &ComponentRegistry::new(), &driver).unwrap();

// The field asserted 4.0 before the run; the held image still carries
// the seed — nothing sees the field until an exchange latches it.
bus.field_set(PointId(1), Value::Float(4.0));
assert_eq!(driver.read(PointId(1)).unwrap().value, Value::Float(0.0));

// One exchange ran at the read boundary and the input phase served the
// fresh latch — while the per-point `read` never transported.
executor.scan();
assert_eq!(bus.exchange_count(), 1);
assert_eq!(executor.sample(PointId(1)).unwrap().value, Value::Float(4.0));

// A write stages into the pending output image — the same call the
// scan's write phase makes — and the *next* scan's exchange publishes
// it: the one-scan actuation delay.
driver.write(PointId(2), Value::Float(7.0)).unwrap();
assert_eq!(bus.field_value(PointId(2)), Some(Value::Float(0.0)));
executor.scan();
assert_eq!(bus.field_value(PointId(2)), Some(Value::Float(7.0)));

// The link drops: exchanges fail, each counted once at the boundary,
// while the held image keeps serving — aging under the point's
// `stale_after_ticks` budget.
bus.set_link_down(true);
bus.field_set(PointId(1), Value::Float(9.0)); // unseen until an exchange lands
executor.scan(); // miss 1 of 3: held value, still inside the budget
assert_eq!(executor.snapshot().io_health.failed_exchanges, 1);
assert_eq!(executor.snapshot().io_health.failed_reads, 0);
assert_eq!(executor.sample(PointId(1)).unwrap().value, Value::Float(4.0));

// Miss 2: the held sample's acquisition stamp lags past the declared
// budget — `Uncertain(Stale)`.
executor.scan();
assert_eq!(
    executor.sample(PointId(1)).unwrap().quality,
    Quality::Uncertain(QualityReason::Stale)
);

// Miss 3 reaches `exchange_miss_threshold`: reads escalate to
// `Disconnected` — an ordinary boundary fault degrading the held value
// to `Bad`.
executor.scan();
let health = &executor.snapshot().io_health;
assert_eq!(health.failed_exchanges, 3);
assert_eq!(health.failed_reads, 1);
assert_eq!(
    executor.sample(PointId(1)).unwrap().quality,
    Quality::Bad(QualityReason::CommunicationFault)
);
let diagnostics = health.driver.clone().unwrap();
assert_eq!(diagnostics.link, LinkState::Disconnected);
assert_eq!(
    diagnostics.exchange.unwrap(),
    ExchangeDiagnostics {
        attempted: 5,
        succeeded: 2,
        working_counter_mismatches: 0,
        last_exchange_tick: Some(Tick(2)),
        missed_deadlines: 0,
    }
);

// The link returns: the next exchange completes — misses reset, the
// link recovers, and the field's asserted value lands fresh.
bus.set_link_down(false);
executor.scan();
assert_eq!(
    executor.sample(PointId(1)).unwrap(),
    Sample::good(Value::Float(9.0), Tick(6))
);

// A `WriteGate` between executor and driver forwards `cyclic()` — the
// exchange is not a write. A quiesced standby still latches fresh
// inputs while its dropped writes never stage.
let gate = dcs_runtime::WriteGate::closed(&driver);
let mut standby = assemble(&model, &ComponentRegistry::new(), &gate).unwrap();
bus.field_set(PointId(1), Value::Float(2.0));
gate.write(PointId(2), Value::Float(5.0)).unwrap(); // accepted and dropped
standby.scan();
assert_eq!(standby.sample(PointId(1)).unwrap().value, Value::Float(2.0));
assert_eq!(bus.field_value(PointId(2)), Some(Value::Float(7.0)));

// Lifting the gate lets the next write stage — and the next exchange
// publish it.
gate.open();
gate.write(PointId(2), Value::Float(5.0)).unwrap();
standby.scan();
assert_eq!(bus.field_value(PointId(2)), Some(Value::Float(5.0)));
```

## Automatic versus supplied

Once the two registrations exist, everything between a model declaration and
a monitored run is platform machinery. The split:

| Automatic — the platform supplies | An integration must supply |
|---|---|
| Port-to-point resolution, including synthesized internal points for port-to-port wires | The `Component` implementation: `step` semantics and quality behavior |
| Requirement checks against the point map — `UnmappedPoint`, `DirectionMismatch`, `TypeMismatch`, `UnboundPort`, `PortBoundTwice`, `UnknownComponentKind` | `KIND` plus `from_parameters` with its parameter validation (`ParameterError`) |
| The scoped `ComponentIo` enforcing declared I/O during `step`, and the deterministic scan order | A `describe()` override for role hints and parameter metadata (a correct default exists) |
| Per-component diagnostics (`step_errors`, `last_error`, `last_tick`) and point samples in `TelemetrySnapshot`, served by `dcs-monitor`'s HTTP+JSON endpoints | `capture_state`/`restore_state` field coverage for every value carried between scans |
| Descriptor publication in `TelemetrySnapshot.descriptors`, so the UI renders any registered kind generically | The registration call in the deployed `ComponentRegistry` |
| The `BlockInterface` derivation, its serving over `GET /schema`/`GET /resources`, invoke submission validation (`UnknownComponent`/`UnknownCommand`/`ArgumentTypeMismatch`), scan-boundary dispatch in submission order, the settled `command_refused` receipt, post-`step` event draining (failing steps included), `event_emitted` journaling at the producing tick, and the emit-identical standby behavior | The `CommandDecl`/`EventDecl` declarations on the descriptor, the `invoke_command`/`drain_events` implementations, the `dcs-build` spec mirror (`declared_commands`/`declared_events`), and `capture_state` coverage of command-mutated and event-sequence state |
| `DeviceSpec` construction, `FanoutDriver` point routing, cross-backend wire routes, and `UnknownDeviceKind` / `InvalidDeviceParameters` / `DeviceBackend` failures naming the device | The `IoDriver` implementation: protocol, timeouts, `IoError` mapping |
| The shared local `SimDriver` merge for `DeviceDriver::Sim` contributions, `FanoutDriver::step(dt)` invoking each backend's `StepHook` (and `step_local(dt)` invoking only non-field-facing hooks), and `FanoutDriver::inspect::<T>` reaching an installed typed handle | Parameter validation (`DeviceError::parameters`), eager backend probing (`DeviceError::backend`), the `field_facing` flag, and the optional `inspect` handle |
| Namespaced per-backend checkpoint state for drivers implementing `capture_state` | The `Sim` vs `Backend` contribution choice, the step hook for simulated kinds, and the capture/restore decision |
| The once-per-scan `exchange` at the read boundary for drivers answering `Some` from `cyclic()`, `failed_exchanges` boundary counting, `stale_after_ticks` aging of held samples, and `WriteGate`/`FanoutDriver` forwarding of the cyclic surface | The cyclic kind's `CyclicIoDriver` implementation: the input/output images, the transport-facing `exchange`, the declared `exchange_miss_threshold`, station attribution of short exchanges, and the `ExchangeDiagnostics` section |

## Where errors surface

Model load reports `LoadError` (`Malformed`, `UnsupportedVersion`, or
`Invalid` listing every `ValidationError`, including `MissingInitial` and
`InitialKindMismatch` for malformed internal points). Assembly reports
`AssemblyError`; the variants an integration can trigger are
`UnknownComponentKind`, `UnboundPort`, `Component` (constructor failures via
`BuildError::other`), `UnmappedPoint`, `DirectionMismatch`, `TypeMismatch`,
`UnknownDeviceKind`, `InvalidDeviceParameters`, `DeviceBackend`, and `Wiring`
for the executor's own check — plus `InvalidInternalPoint` and
`MixedPointLink` for malformed channel-less points, which a validated model
reports earlier as `ValidationError`s. Runtime failures land in per-component `ComponentDiagnostics` and
driver `IoError`s rather than panics. See
`crates/dcs-assembly/tests/drivers.rs` and
`crates/dcs-assembly/tests/assembly.rs` for tests exercising each named
failure.

## Testing an integration

Unit-test a component by stepping it against a scoped `ComponentIo` stub —
`dcs-blocks`' `testutil::TestIo` (`crates/dcs-blocks/src/lib.rs`) is the
pattern — then an assembly test like the worked examples above proves
registration, wiring checks, and scanning end to end. Test a device factory
through `resolve_drivers` with a model document: unknown kinds, rejected
parameters, and unservable backends all surface as named `AssemblyError`s
before the first scan, exactly as `crates/dcs-assembly/tests/drivers.rs`
exercises for `sim-tcp`.
