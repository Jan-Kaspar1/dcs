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
descriptor per component in `TelemetrySnapshot.descriptors`.

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
restoring it. A revision that wants an instance's state to survive keeps
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
`{"Float": …}` / `{"Int": …}` / `{"Bool": …}` object), and the `ports`
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
          "level": {"direction": "in", "value_type": "Float"},
          "reset": {"direction": "in", "value_type": "Bool"},
          "peak":  {"direction": "out", "value_type": "Float"}
        }
      }],
      "io_points": [
        {"id": 1, "direction": "in",  "value_type": "Float",
         "channel": {"device": 1, "name": "level"}},
        {"id": 2, "direction": "in",  "value_type": "Bool",
         "channel": {"device": 1, "name": "reset"}},
        {"id": 3, "direction": "out", "value_type": "Float",
         "channel": {"device": 1, "name": "peak"}}
      ],
      "signals": [{"id": 1, "name": "level-peak", "source": 3}],
      "components": [{
        "id": 1, "kind": "running-max",
        "parameters": {"initial": {"Float": 0.0}},
        "ports": {
          "in":    {"direction": "in",  "value_type": "Float"},
          "reset": {"direction": "in",  "value_type": "Bool"},
          "out":   {"direction": "out", "value_type": "Float"}
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
executor.scan().unwrap();
assert_eq!(driver.read(PointId(3)).unwrap().value, Value::Float(5.0));

driver.write(PointId(1), Value::Float(3.0)).unwrap();
executor.scan().unwrap();
assert_eq!(driver.read(PointId(3)).unwrap().value, Value::Float(5.0));

driver.write(PointId(2), Value::Bool(true)).unwrap();
executor.scan().unwrap();
assert_eq!(driver.read(PointId(3)).unwrap().value, Value::Float(3.0));

// The snapshot carries the instance's descriptor and diagnostics.
assert_eq!(executor.snapshot().descriptors[0].kind, "running-max");
assert_eq!(executor.snapshot().components[0].step_errors, 0);
```

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
- `DeviceDriver::Backend(DeviceBackend { io, step, claim, inspect, field_facing })` — a
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
          "raw": {"direction": "in",  "value_type": "Float"},
          "eng": {"direction": "out", "value_type": "Float"}
        }
      }],
      "io_points": [
        {"id": 1, "direction": "in",  "value_type": "Float",
         "channel": {"device": 1, "name": "raw"}},
        {"id": 2, "direction": "out", "value_type": "Float",
         "channel": {"device": 1, "name": "eng"}}
      ],
      "signals": [{"id": 1, "name": "level-eng", "source": 2}],
      "components": [{
        "id": 1, "kind": "analog-input",
        "parameters": {
          "raw_min": {"Float": 0.0}, "raw_max": {"Float": 10.0},
          "eng_min": {"Float": 0.0}, "eng_max": {"Float": 100.0}
        },
        "ports": {
          "raw": {"direction": "in",  "value_type": "Float"},
          "out": {"direction": "out", "value_type": "Float"}
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
executor.scan().unwrap();
assert_eq!(driver.read(PointId(2)).unwrap().value, Value::Float(50.0));

// The plant side moves the input; the next scan follows.
driver.write(PointId(1), Value::Float(10.0)).unwrap();
executor.scan().unwrap();
assert_eq!(driver.read(PointId(2)).unwrap().value, Value::Float(100.0));
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
| `DeviceSpec` construction, `FanoutDriver` point routing, cross-backend wire routes, and `UnknownDeviceKind` / `InvalidDeviceParameters` / `DeviceBackend` failures naming the device | The `IoDriver` implementation: protocol, timeouts, `IoError` mapping |
| The shared local `SimDriver` merge for `DeviceDriver::Sim` contributions, `FanoutDriver::step(dt)` invoking each backend's `StepHook` (and `step_local(dt)` invoking only non-field-facing hooks), and `FanoutDriver::inspect::<T>` reaching an installed typed handle | Parameter validation (`DeviceError::parameters`), eager backend probing (`DeviceError::backend`), the `field_facing` flag, and the optional `inspect` handle |
| Namespaced per-backend checkpoint state for drivers implementing `capture_state` | The `Sim` vs `Backend` contribution choice, the step hook for simulated kinds, and the capture/restore decision |

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
