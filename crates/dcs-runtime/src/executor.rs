//! The cyclic executor: wiring verification, the scan image, and the
//! virtual tick.
//!
//! [`Executor::new`] checks every component's declared I/O against the
//! driver's [`PointMap`] before anything runs; [`Executor::scan`] and
//! [`Executor::run`] then advance a virtual [`Tick`] and cycle read → step
//! → write deterministically.

use crate::checkpoint::{Checkpoint, RestoreError};
use crate::component::{Component, ComponentIo, IoRequirement};
use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, ComponentDiagnostics, Direction,
    IoDriver, IoError, PointId, PointTelemetry, Quality, QualityReason, Sample, TelemetrySnapshot,
    Tick, Value, ValueKind,
};
use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::fmt;

/// How the controller may use one mapped point.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct PointSpec {
    /// `In` points are read into the scan image; `Out` points are written
    /// from the image to the driver.
    pub direction: Direction,
    /// The point's declared value kind.
    pub kind: ValueKind,
    /// `Some(initial)` marks an internal point: image-carried rather than
    /// driver-served. The image is seeded with `initial` at wiring and the
    /// point holds it until written — by a command through the command
    /// path for `In`, by a component for `Out`. `None` marks a field
    /// point the driver serves.
    pub internal: Option<Value>,
}

/// The executor's point map: which logical points exist, whether the
/// driver or the scan image serves each, and the internal links routing
/// values between image-carried points.
///
/// The map is the resolved form of the plant model's I/O mapping — the
/// caller that assembled the run supplies it, and the executor treats it
/// as the authority every component's declared I/O is checked against at
/// wiring time.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct PointMap {
    points: BTreeMap<PointId, PointSpec>,
    /// Internal `Out` → `In` routes through the scan image: at each
    /// scan's input phase the `Out` point's sample is copied onto the
    /// `In` point — the image-carried carrier for port-to-port and
    /// internal point-to-point wiring.
    links: Vec<(PointId, PointId)>,
}

impl PointMap {
    /// An empty map.
    pub fn new() -> Self {
        Self::default()
    }

    /// Adds `point` as a driver-served field point with the given
    /// direction and value kind; a repeated id replaces the earlier spec.
    pub fn with_point(mut self, point: PointId, direction: Direction, kind: ValueKind) -> Self {
        self.points.insert(
            point,
            PointSpec {
                direction,
                kind,
                internal: None,
            },
        );
        self
    }

    /// Adds `point` as an image-carried internal point holding `initial`
    /// until it is written; a repeated id replaces the earlier spec. The
    /// driver is never touched for an internal point.
    pub fn with_internal(
        mut self,
        point: PointId,
        direction: Direction,
        kind: ValueKind,
        initial: Value,
    ) -> Self {
        self.points.insert(
            point,
            PointSpec {
                direction,
                kind,
                internal: Some(initial),
            },
        );
        self
    }

    /// Routes internal `Out` point `output`'s image sample onto internal
    /// `In` point `input` at each scan's input phase — the image-carried
    /// equivalent of a field-side loopback, delivering the value one scan
    /// after it was written. [`Executor::new`] rejects a link whose ends
    /// are not mapped internal `Out`/`In` points of one kind.
    pub fn with_internal_link(mut self, output: PointId, input: PointId) -> Self {
        self.links.push((output, input));
        self
    }

    /// The spec for `point`, or `None` when the map does not serve it.
    pub fn get(&self, point: PointId) -> Option<PointSpec> {
        self.points.get(&point).copied()
    }

    /// Iterates the mapped points in ascending id order.
    pub fn iter(&self) -> impl Iterator<Item = (PointId, PointSpec)> + '_ {
        self.points.iter().map(|(&point, &spec)| (point, spec))
    }

    /// The declared internal links — `(output, input)` pairs — in
    /// declaration order.
    pub fn links(&self) -> impl Iterator<Item = (PointId, PointId)> + '_ {
        self.links.iter().copied()
    }
}

impl FromIterator<(PointId, Direction, ValueKind)> for PointMap {
    fn from_iter<I: IntoIterator<Item = (PointId, Direction, ValueKind)>>(iter: I) -> Self {
        let mut map = Self::new();
        for (point, direction, kind) in iter {
            map = map.with_point(point, direction, kind);
        }
        map
    }
}

/// Why a [`PointMap`] internal link is rejected at wiring: a link must
/// route a mapped internal `Out` point onto a mapped internal `In` point
/// of the same value kind, driving each `In` point at most once.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum LinkError {
    /// The `output` end is not a mapped internal `Out` point — it is
    /// unmapped, field-backed, or an `In` point.
    Output,
    /// The `input` end is not a mapped internal `In` point — it is
    /// unmapped, field-backed, or an `Out` point.
    Input,
    /// The link's ends carry different value kinds.
    KindMismatch,
    /// The `input` point is already driven by another link.
    Conflict,
}

impl fmt::Display for LinkError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            LinkError::Output => "the output end is not an internal out point",
            LinkError::Input => "the input end is not an internal in point",
            LinkError::KindMismatch => "the ends carry different value kinds",
            LinkError::Conflict => "the input point is already driven by another link",
        })
    }
}

/// Why an executor refuses to run: a component's declared I/O does not
/// match the driver's point map, or an internal link is malformed. Every
/// variant names the offending element.
#[derive(Debug, Clone, PartialEq)]
pub enum WiringError {
    /// Two components declare the same name. Names are each component's
    /// identity within a run — diagnostics and the
    /// [`Checkpoint`](crate::Checkpoint) component keys rely on them being
    /// unique.
    DuplicateComponent {
        /// The repeated component name.
        component: String,
    },
    /// A component declares a point the map does not serve.
    UnknownPoint {
        /// The component declaring the point.
        component: String,
        /// The point the map does not serve.
        point: PointId,
    },
    /// A component declares one point more than once.
    DuplicateDeclaration {
        /// The component with the repeated declaration.
        component: String,
        /// The point declared more than once.
        point: PointId,
    },
    /// A component's declared direction differs from the map's.
    DirectionMismatch {
        /// The component declaring the point.
        component: String,
        /// The mismatched point.
        point: PointId,
        /// The direction the component declared.
        declared: Direction,
        /// The direction the map declares.
        mapped: Direction,
    },
    /// A component's declared value kind differs from the map's.
    TypeMismatch {
        /// The component declaring the point.
        component: String,
        /// The mismatched point.
        point: PointId,
        /// The value kind the component declared.
        declared: ValueKind,
        /// The value kind the map declares.
        mapped: ValueKind,
    },
    /// An internal link in the point map is malformed.
    InvalidLink {
        /// The link's producing point.
        output: PointId,
        /// The link's consuming point.
        input: PointId,
        /// What the link violates.
        detail: LinkError,
    },
}

impl fmt::Display for WiringError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicateComponent { component } => {
                write!(
                    f,
                    "component name {component:?} is registered more than once"
                )
            }
            Self::UnknownPoint { component, point } => write!(
                f,
                "component {component:?} declares io point {} the driver map does not serve",
                point.0
            ),
            Self::DuplicateDeclaration { component, point } => write!(
                f,
                "component {component:?} declares io point {} more than once",
                point.0
            ),
            Self::DirectionMismatch {
                component,
                point,
                declared,
                mapped,
            } => write!(
                f,
                "component {component:?} declares io point {} as {declared} but the driver map has {mapped}",
                point.0
            ),
            Self::TypeMismatch {
                component,
                point,
                declared,
                mapped,
            } => write!(
                f,
                "component {component:?} declares io point {} as {declared:?} but the driver map has {mapped:?}",
                point.0
            ),
            Self::InvalidLink {
                output,
                input,
                detail,
            } => write!(
                f,
                "internal link {} -> {} is invalid: {detail}",
                output.0, input.0
            ),
        }
    }
}

impl std::error::Error for WiringError {}

/// Why a scan failed after the step phase.
///
/// Component `step` errors never fail a scan — they are counted per
/// component in [`Executor::component_statuses`]. `ScanError` covers the
/// driver boundary: the output image could not be delivered to the field.
#[derive(Debug, Clone, PartialEq)]
pub enum ScanError {
    /// Writing the output image to the driver failed.
    Io(IoError),
}

impl fmt::Display for ScanError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(f, "output write failed: {error}"),
        }
    }
}

impl std::error::Error for ScanError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
        }
    }
}

impl From<IoError> for ScanError {
    fn from(error: IoError) -> Self {
        Self::Io(error)
    }
}

/// Runtime diagnostics for one registered component.
#[derive(Debug, Clone, PartialEq)]
pub struct ComponentStatus {
    /// The component's [`name`](Component::name).
    pub name: String,
    /// The last tick the component stepped without error, if any.
    pub last_tick: Option<Tick>,
    /// How many `step` calls have failed.
    pub step_errors: u64,
    /// The most recent `step` error's message, if any.
    pub last_error: Option<String>,
}

/// A registered component plus its verified declaration set and
/// diagnostics.
struct Entry {
    component: Box<dyn Component>,
    /// Declared requirements indexed by point; the step's scoped I/O view
    /// checks every access against this set.
    declared: HashMap<PointId, IoRequirement>,
    last_tick: Option<Tick>,
    step_errors: u64,
    last_error: Option<String>,
}

/// The scoped [`ComponentIo`] a component sees during `step`: the scan
/// image restricted to the component's declared points and directions.
struct ScopedIo<'a> {
    image: &'a RefCell<HashMap<PointId, Sample>>,
    declared: &'a HashMap<PointId, IoRequirement>,
    tick: Tick,
}

impl ScopedIo<'_> {
    /// Resolves `point` when the component declared it with `direction`;
    /// undeclared or wrong-direction access is `UnknownPoint`.
    fn requirement(&self, point: PointId, direction: Direction) -> Result<(), IoError> {
        match self.declared.get(&point) {
            Some(requirement) if requirement.direction == direction => Ok(()),
            _ => Err(IoError::UnknownPoint(point)),
        }
    }
}

impl IoDriver for ScopedIo<'_> {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        self.requirement(point, Direction::In)?;
        // Wiring guarantees the point is an `In` point of the map, so the
        // input-read phase always leaves an image entry.
        self.image
            .borrow()
            .get(&point)
            .copied()
            .ok_or(IoError::UnknownPoint(point))
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        self.write_sample(point, Sample::good(value, self.tick))
    }
}

impl ComponentIo for ScopedIo<'_> {
    fn write_sample(&self, point: PointId, sample: Sample) -> Result<(), IoError> {
        self.requirement(point, Direction::Out)?;
        let kind = self.declared[&point].kind;
        if sample.value.kind() != kind {
            return Err(IoError::TypeMismatch {
                point,
                expected: kind,
                found: sample.value,
            });
        }
        self.image.borrow_mut().insert(
            point,
            Sample {
                tick: self.tick,
                ..sample
            },
        );
        Ok(())
    }
}

/// The value stored for a point whose first read already failed.
fn neutral(kind: ValueKind) -> Value {
    match kind {
        ValueKind::Bool => Value::Bool(false),
        ValueKind::Int => Value::Int(0),
        ValueKind::Float => Value::Float(0.0),
    }
}

/// The quality stamped on a sample whose input read failed: field-side
/// faults degrade control inputs to `Bad` — they never abort the scan.
fn failure_quality(error: IoError) -> Quality {
    match error {
        IoError::Disconnected(_) | IoError::Timeout(_) => {
            Quality::Bad(QualityReason::CommunicationFault)
        }
        IoError::UnknownPoint(_) | IoError::TypeMismatch { .. } => {
            Quality::Bad(QualityReason::ConfigurationFault)
        }
    }
}

/// A deterministic fixed-step executor over registered components.
///
/// The scan order is the order `components` were registered in — explicit
/// and configured by the caller. Each [`scan`](Executor::scan):
///
/// 1. advances the virtual tick by one — the executor's tick is the only
///    timestamp authority;
/// 2. applies every queued operator [`Command`] in submission order —
///    this is the documented point where commands submitted between scans
///    take effect, each updating its receipt to the final outcome;
/// 3. refreshes the image's `In` points: every field `In` point is read
///    from the driver, stamping the new tick — a failed read keeps the
///    last known value marked [`Quality::Bad`] rather than aborting the
///    scan — and every internal link routes its `Out` point's image
///    sample onto its `In` point, so a port-to-port carrier delivers the
///    value one scan after it was written;
/// 4. steps the components in scan order, each seeing a [`ComponentIo`]
///    scoped to its declared points — a failing step is recorded and the
///    scan continues;
/// 5. writes the image's field `Out` points to the driver — points a
///    component never wrote keep their last output, so a failed step
///    holds outputs.
///
/// Internal points — declared in the map via
/// [`PointMap::with_internal`] — are served by the image alone: the
/// driver is never read or written for them. An internal `In` point
/// holds its declared initial value until a command writes it through
/// the command path; an internal `Out` point records component writes
/// for monitoring, keeping the last written sample visible in
/// [`snapshot`](Executor::snapshot) and
/// [`checkpoint`](Executor::checkpoint).
///
/// Applying commands before the input read gives a command to an `In`
/// point setpoint semantics: the same scan's input phase observes the
/// written value, and it holds for later scans until the field-side value
/// changes again — for an internal `In` point, until the next command.
/// A command to an `Out` point lands in the image where a component write
/// later in the same scan may still override it — components win, keeping
/// control logic authoritative inside a scan — and a link routing from
/// the commanded point carries the value to its `In` target that same
/// scan.
///
/// Nothing reads a wall clock: identical driver behavior over identical
/// scans produces identical samples on every host.
pub struct Executor<'d> {
    driver: &'d (dyn IoDriver + Sync),
    map: PointMap,
    components: Vec<Entry>,
    image: RefCell<HashMap<PointId, Sample>>,
    /// Indices into `receipts` of the queued commands awaiting their scan
    /// boundary; the command itself rides inside its receipt.
    pending_commands: VecDeque<usize>,
    receipts: Vec<CommandReceipt>,
    tick: Tick,
}

impl<'d> Executor<'d> {
    /// Wires `components` against `map` and returns a runnable executor.
    ///
    /// Every [`IoRequirement`] of every component must resolve against the
    /// map: the point must be served and the declared direction and value
    /// kind must match exactly. The first violation stops wiring with a
    /// [`WiringError`] naming the component and point; nothing runs.
    /// Components step in `components` order — the configured scan order.
    ///
    /// The driver must be [`Sync`] so the executor can be shared across
    /// threads — e.g. behind the monitoring server's mutex — while tests
    /// and fault injectors reach it through their own references.
    pub fn new(
        driver: &'d (dyn IoDriver + Sync),
        map: PointMap,
        components: Vec<Box<dyn Component>>,
    ) -> Result<Self, WiringError> {
        let mut entries = Vec::with_capacity(components.len());
        let mut names = HashSet::with_capacity(components.len());
        for component in components {
            let component_name = || component.name().to_string();
            if !names.insert(component.name().to_string()) {
                return Err(WiringError::DuplicateComponent {
                    component: component_name(),
                });
            }
            let mut declared = HashMap::new();
            for requirement in component.io_requirements() {
                let point = requirement.point;
                if declared.contains_key(&point) {
                    return Err(WiringError::DuplicateDeclaration {
                        component: component_name(),
                        point,
                    });
                }
                let spec = map.get(point).ok_or(WiringError::UnknownPoint {
                    component: component_name(),
                    point,
                })?;
                if requirement.direction != spec.direction {
                    return Err(WiringError::DirectionMismatch {
                        component: component_name(),
                        point,
                        declared: requirement.direction,
                        mapped: spec.direction,
                    });
                }
                if requirement.kind != spec.kind {
                    return Err(WiringError::TypeMismatch {
                        component: component_name(),
                        point,
                        declared: requirement.kind,
                        mapped: spec.kind,
                    });
                }
                declared.insert(point, requirement);
            }
            entries.push(Entry {
                component,
                declared,
                last_tick: None,
                step_errors: 0,
                last_error: None,
            });
        }

        // Internal links are wiring too: each must route a mapped internal
        // `Out` point onto a mapped internal `In` point of the same kind,
        // driving it at most once.
        let mut driven = HashSet::with_capacity(map.links.len());
        for (output, input) in map.links() {
            let internal = |point: PointId, direction: Direction| {
                map.get(point)
                    .is_some_and(|spec| spec.direction == direction && spec.internal.is_some())
            };
            let detail = if !internal(output, Direction::Out) {
                Some(LinkError::Output)
            } else if !internal(input, Direction::In) {
                Some(LinkError::Input)
            } else if map.get(output).unwrap().kind != map.get(input).unwrap().kind {
                Some(LinkError::KindMismatch)
            } else if !driven.insert(input) {
                Some(LinkError::Conflict)
            } else {
                None
            };
            if let Some(detail) = detail {
                return Err(WiringError::InvalidLink {
                    output,
                    input,
                    detail,
                });
            }
        }

        // Internal points are seeded into the image at their declared
        // initial values — a held operator value or a carrier's start —
        // so every internal point has a defined sample before the first
        // scan.
        let image = RefCell::new(HashMap::new());
        for (point, spec) in map.iter() {
            if let Some(initial) = spec.internal {
                image
                    .borrow_mut()
                    .insert(point, Sample::good(initial, Tick::ZERO));
            }
        }
        Ok(Self {
            driver,
            map,
            components: entries,
            image,
            pending_commands: VecDeque::new(),
            receipts: Vec::new(),
            tick: Tick::ZERO,
        })
    }

    /// The executor's current virtual tick: [`Tick::ZERO`] before the first
    /// scan, thereafter the tick the last scan ran at.
    pub fn tick(&self) -> Tick {
        self.tick
    }

    /// Diagnostics for each registered component, in scan order.
    pub fn component_statuses(&self) -> Vec<ComponentStatus> {
        self.components
            .iter()
            .map(|entry| ComponentStatus {
                name: entry.component.name().to_string(),
                last_tick: entry.last_tick,
                step_errors: entry.step_errors,
                last_error: entry.last_error.clone(),
            })
            .collect()
    }

    /// The image's latest sample for `point`, if the scan has produced one.
    ///
    /// Components see the same image through `step`; this exposes it for
    /// monitoring and tests.
    pub fn sample(&self, point: PointId) -> Option<Sample> {
        self.image.borrow().get(&point).copied()
    }

    /// A monitoring snapshot of the run: the executor's tick, the latest
    /// image sample of every mapped point, and per-component diagnostics.
    ///
    /// This is the read-side half of the unified contract — the returned
    /// [`TelemetrySnapshot`] is defined in `dcs-core` and
    /// serde-serializable, so a monitoring UI consumes it without
    /// depending on the runtime. `In` points report the scan's input
    /// read, `Out` points the last staged write; a mapped point no scan
    /// has produced a sample for — an output no component has written —
    /// reports `None`. Points are ordered by ascending id and components
    /// by scan order, so equal runs snapshot identically.
    ///
    /// The snapshot's `descriptors` carry each component's
    /// [`describe`](Component::describe) result in the same scan order as
    /// `components` — one descriptor per registered component, so
    /// `descriptors[i]` describes the component `components[i]`
    /// diagnoses.
    pub fn snapshot(&self) -> TelemetrySnapshot {
        let image = self.image.borrow();
        TelemetrySnapshot {
            tick: self.tick,
            points: self
                .map
                .iter()
                .map(|(point, spec)| PointTelemetry {
                    point,
                    direction: spec.direction,
                    sample: image.get(&point).copied(),
                })
                .collect(),
            components: self
                .components
                .iter()
                .map(|entry| ComponentDiagnostics {
                    name: entry.component.name().to_string(),
                    last_tick: entry.last_tick,
                    step_errors: entry.step_errors,
                    last_error: entry.last_error.clone(),
                })
                .collect(),
            descriptors: self
                .components
                .iter()
                .map(|entry| entry.component.describe())
                .collect(),
        }
    }

    /// Queues `command` for application at the start of the next scan and
    /// returns its receipt.
    ///
    /// Submission validates the command against the point map — the point
    /// must be served, and the declared kind must match both the map's
    /// kind and the supplied value's variant — so an invalid command is
    /// [`CommandOutcome::Rejected`] immediately and never queued. An
    /// [`CommandOutcome::Accepted`] receipt reports the tick the command
    /// is scheduled to apply at; at the head of the next
    /// [`scan`](Executor::scan), before the input-read phase, that same
    /// log entry's outcome is updated to [`CommandOutcome::Applied`], or
    /// `Rejected` with [`CommandError::DriverRejected`] when the field
    /// driver refuses the write — exactly one receipt per command, kept
    /// in the [`receipts`](Executor::receipts) log in submission order.
    pub fn submit_command(&mut self, command: Command) -> CommandReceipt {
        let outcome = match self.check_command(command) {
            Err(reason) => CommandOutcome::Rejected { reason },
            Ok(_) => CommandOutcome::Accepted {
                apply_tick: Tick(self.tick.0 + 1),
            },
        };
        let receipt = CommandReceipt { command, outcome };
        self.receipts.push(receipt);
        if matches!(outcome, CommandOutcome::Accepted { .. }) {
            self.pending_commands.push_back(self.receipts.len() - 1);
        }
        receipt
    }

    /// The receipt log: one receipt per submitted command, in submission
    /// order.
    ///
    /// A queued command's entry reads [`CommandOutcome::Accepted`] until
    /// the scan boundary updates it to `Applied` or `Rejected`, so the
    /// log is the audit trail a monitoring consumer reads alongside the
    /// snapshot.
    pub fn receipts(&self) -> &[CommandReceipt] {
        &self.receipts
    }

    /// Runs `scans` scans and returns the tick the last one ran at.
    ///
    /// `run(0)` is a no-op returning the current tick. A [`ScanError`]
    /// aborts the run partway; the tick reports how far it got.
    pub fn run(&mut self, scans: u64) -> Result<Tick, ScanError> {
        for _ in 0..scans {
            self.scan()?;
        }
        Ok(self.tick)
    }

    /// Executes one scan — apply commands, read inputs, step components,
    /// write outputs — and returns the tick it ran at. See the type docs
    /// for the phase order.
    pub fn scan(&mut self) -> Result<Tick, ScanError> {
        self.tick = Tick(self.tick.0 + 1);
        let tick = self.tick;

        self.apply_commands(tick);
        self.read_inputs(tick);
        self.step_components(tick);
        self.write_outputs()?;
        Ok(tick)
    }

    /// Captures the run's transferable state as a [`Checkpoint`].
    ///
    /// The checkpoint bundles the current tick, every component's
    /// [`capture_state`](Component::capture_state) keyed by name (empty
    /// for stateless components), the driver's captured state when it
    /// implements the contract, and the image-carried point samples: the
    /// `Out` samples — the last written output values — plus the internal
    /// `In` samples, so held operator values and link carriers transfer.
    /// It is serde-serializable, so an active
    /// controller can ship it to a standby over the same JSON channel the
    /// monitoring contract uses. See the [`Checkpoint`] docs for how this
    /// maps to real redundancy.
    pub fn checkpoint(&self) -> Checkpoint {
        let image = self.image.borrow();
        Checkpoint {
            tick: self.tick,
            components: self
                .components
                .iter()
                .map(|entry| {
                    (
                        entry.component.name().to_string(),
                        entry.component.capture_state(),
                    )
                })
                .collect(),
            driver: self.driver.capture_state(),
            outputs: self
                .map
                .iter()
                .filter(|(_, spec)| spec.direction == Direction::Out)
                .filter_map(|(point, _)| image.get(&point).map(|sample| (point, *sample)))
                .collect(),
            internal: self
                .map
                .iter()
                .filter(|(_, spec)| spec.direction == Direction::In && spec.internal.is_some())
                .filter_map(|(point, _)| image.get(&point).map(|sample| (point, *sample)))
                .collect(),
        }
    }

    /// Rebuilds an executor equivalent to the one `checkpoint` captured.
    ///
    /// `driver`, `map`, and `components` are the same inputs
    /// [`Executor::new`] takes — on a standby they are built from the same
    /// plant model. Restore wires the components, checks that the
    /// checkpoint's component set equals the registered set by name,
    /// applies the driver state when the checkpoint carries one, restores
    /// each component's state, then resumes the tick and the output image.
    /// The result is an executor whose next [`scan`](Executor::scan)
    /// produces outputs identical to the captured run's.
    ///
    /// Any incompatibility fails with a [`RestoreError`] naming the
    /// element at fault: a [`WiringError`], a component-name mismatch, a
    /// component's [`StateError`](dcs_core::StateError), or the driver's.
    /// No partially restored executor is returned; the supplied driver is
    /// asked to validate before applying its state section.
    pub fn restore(
        driver: &'d (dyn IoDriver + Sync),
        map: PointMap,
        components: Vec<Box<dyn Component>>,
        checkpoint: &Checkpoint,
    ) -> Result<Self, RestoreError> {
        let mut executor = Self::new(driver, map, components)?;

        // Component names are the run's component ids: the checkpoint's
        // set must equal the registered set exactly.
        for component in checkpoint.components.keys() {
            if !executor
                .components
                .iter()
                .any(|entry| entry.component.name() == component)
            {
                return Err(RestoreError::UnknownComponent {
                    component: component.clone(),
                });
            }
        }
        for entry in &executor.components {
            if !checkpoint.components.contains_key(entry.component.name()) {
                return Err(RestoreError::MissingComponent {
                    component: entry.component.name().to_string(),
                });
            }
        }

        // Driver state before component state: a driver that does not
        // implement the contract rejects a captured section before any
        // component is touched.
        if let Some(state) = &checkpoint.driver {
            driver.restore_state(state).map_err(RestoreError::Driver)?;
        }

        for entry in &mut executor.components {
            let state = checkpoint
                .components
                .get(entry.component.name())
                .expect("checked above");
            entry
                .component
                .restore_state(state)
                .map_err(RestoreError::Component)?;
        }

        executor.tick = checkpoint.tick;
        executor.image.borrow_mut().extend(
            checkpoint
                .outputs
                .iter()
                .chain(checkpoint.internal.iter())
                .map(|(&point, &sample)| (point, sample)),
        );
        Ok(executor)
    }

    /// Validates `command` against the point map, returning the target
    /// point and value to write. The checks are static — the map fixes
    /// which points exist and their declared kinds — so the same check at
    /// submission and at application can only differ when the driver
    /// refuses the write itself.
    fn check_command(&self, command: Command) -> Result<(PointId, Value), CommandError> {
        match command {
            Command::WriteValue { point, kind, value } => {
                let spec = self
                    .map
                    .get(point)
                    .ok_or(CommandError::UnknownPoint { point })?;
                if kind != spec.kind {
                    return Err(CommandError::TypeMismatch {
                        point,
                        expected: spec.kind,
                        found: value,
                    });
                }
                if value.kind() != kind {
                    return Err(CommandError::TypeMismatch {
                        point,
                        expected: kind,
                        found: value,
                    });
                }
                Ok((point, value))
            }
        }
    }

    /// Applies every queued command in submission order, updating each
    /// one's receipt to its final outcome. Runs at the head of the scan —
    /// before the input read — so a write to an `In` point is observed by
    /// this scan's input phase, while a write to an `Out` point enters the
    /// image where the step phase may still override it.
    ///
    /// A command to an internal point writes the image directly — the
    /// driver does not serve it — so a held `In` value changes here and
    /// holds until the next command, and a write to an `Out` point routes
    /// through its internal links at this same scan's input phase.
    fn apply_commands(&mut self, tick: Tick) {
        while let Some(index) = self.pending_commands.pop_front() {
            let command = self.receipts[index].command;
            self.receipts[index].outcome = match self.check_command(command) {
                Err(reason) => CommandOutcome::Rejected { reason },
                Ok((point, value)) => {
                    let internal = self
                        .map
                        .get(point)
                        .is_some_and(|spec| spec.internal.is_some());
                    if internal {
                        self.image
                            .borrow_mut()
                            .insert(point, Sample::good(value, tick));
                        CommandOutcome::Applied { tick }
                    } else {
                        match self.driver.write(point, value) {
                            Err(error) => CommandOutcome::Rejected {
                                reason: CommandError::DriverRejected { point, error },
                            },
                            Ok(()) => {
                                self.image
                                    .borrow_mut()
                                    .insert(point, Sample::good(value, tick));
                                CommandOutcome::Applied { tick }
                            }
                        }
                    }
                }
            };
        }
    }

    /// Refreshes the image's `In` points for the scan: every field `In`
    /// point is read from the driver, stamping `tick` — a failed read
    /// keeps the last known value, a neutral one if none, marked `Bad`,
    /// so a field fault degrades inputs instead of stopping the
    /// controller — while every internal link routes its `Out` point's
    /// image sample onto its `In` point, delivering the value one scan
    /// after it was written. Held internal `In` points — unlinked —
    /// keep their image value untouched.
    fn read_inputs(&mut self, tick: Tick) {
        let mut image = self.image.borrow_mut();
        for (point, spec) in self.map.iter() {
            if spec.direction != Direction::In || spec.internal.is_some() {
                continue;
            }
            let sample = match self.driver.read(point) {
                Ok(sample) => Sample { tick, ..sample },
                Err(error) => Sample::new(
                    image
                        .get(&point)
                        .map_or(neutral(spec.kind), |last| last.value),
                    failure_quality(error),
                    tick,
                ),
            };
            image.insert(point, sample);
        }
        for (output, input) in self.map.links() {
            if let Some(sample) = image.get(&output).copied() {
                image.insert(input, Sample { tick, ..sample });
            }
        }
    }

    /// Steps each component in scan order over an image view scoped to its
    /// declared points. A failing step is recorded and the scan continues.
    fn step_components(&mut self, tick: Tick) {
        let image = &self.image;
        for entry in &mut self.components {
            let io = ScopedIo {
                image,
                declared: &entry.declared,
                tick,
            };
            match entry.component.step(&io, tick) {
                Ok(()) => entry.last_tick = Some(tick),
                Err(error) => {
                    entry.step_errors += 1;
                    entry.last_error = Some(error.to_string());
                }
            }
        }
    }

    /// Writes every field `Out` point the image holds to the driver.
    /// Points a component never wrote keep no image entry and are left
    /// untouched; internal `Out` points are image-carried for monitoring
    /// and never reach the driver.
    fn write_outputs(&mut self) -> Result<(), ScanError> {
        let image = self.image.borrow();
        for (point, spec) in self.map.iter() {
            if spec.direction != Direction::Out || spec.internal.is_some() {
                continue;
            }
            let Some(sample) = image.get(&point) else {
                continue;
            };
            self.driver.write(point, sample.value)?;
        }
        Ok(())
    }
}

impl fmt::Debug for Executor<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Executor")
            .field("tick", &self.tick)
            .field(
                "components",
                &self
                    .components
                    .iter()
                    .map(|entry| entry.component.name())
                    .collect::<Vec<_>>(),
            )
            .finish_non_exhaustive()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{ComponentIoExt, StepError};
    use dcs_core::{
        ComponentDescriptor, Input, Output, ParameterDescriptor, ParameterRange, PortDescriptor,
        PortRole,
    };
    use std::collections::HashSet;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::{Arc, Mutex};

    /// In-memory driver stub with deferred `Out`→`In` routing: writes land
    /// immediately, and `advance` copies each loopback's output sample onto
    /// its input — the same scan-boundary semantics `dcs-sim` gives its
    /// `Loopback`s. Samples carry the stub's own tick counter; the executor
    /// re-stamps them into its domain on read.
    struct StubDriver {
        points: Mutex<HashMap<PointId, Sample>>,
        loopbacks: Vec<(PointId, PointId)>,
        faults: Mutex<HashSet<PointId>>,
        tick: AtomicU64,
    }

    impl StubDriver {
        fn new(points: &[(PointId, Value)], loopbacks: &[(PointId, PointId)]) -> Self {
            Self {
                points: Mutex::new(
                    points
                        .iter()
                        .map(|&(point, value)| (point, Sample::good(value, Tick::ZERO)))
                        .collect(),
                ),
                loopbacks: loopbacks.to_vec(),
                faults: Mutex::new(HashSet::new()),
                tick: AtomicU64::new(0),
            }
        }

        /// Routes pending loopbacks and advances the driver's own tick.
        fn advance(&self) {
            let tick = Tick(self.tick.fetch_add(1, Ordering::Relaxed) + 1);
            let mut points = self.points.lock().unwrap();
            for &(output, input) in &self.loopbacks {
                let sample = points[&output];
                points.insert(input, Sample { tick, ..sample });
            }
        }
    }

    impl IoDriver for StubDriver {
        fn read(&self, point: PointId) -> Result<Sample, IoError> {
            if self.faults.lock().unwrap().contains(&point) {
                return Err(IoError::Disconnected(point));
            }
            self.points
                .lock()
                .unwrap()
                .get(&point)
                .copied()
                .ok_or(IoError::UnknownPoint(point))
        }

        fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
            if self.faults.lock().unwrap().contains(&point) {
                return Err(IoError::Disconnected(point));
            }
            let mut points = self.points.lock().unwrap();
            let sample = points.get_mut(&point).ok_or(IoError::UnknownPoint(point))?;
            if value.kind() != sample.value.kind() {
                return Err(IoError::TypeMismatch {
                    point,
                    expected: sample.value.kind(),
                    found: value,
                });
            }
            *sample = Sample::good(value, Tick(self.tick.load(Ordering::Relaxed)));
            Ok(())
        }
    }

    /// Reads a `Float` input and writes it scaled by `gain` to a `Float`
    /// output.
    struct Scale {
        name: &'static str,
        input: PointId,
        output: PointId,
        gain: f64,
    }

    impl Component for Scale {
        fn name(&self) -> &str {
            self.name
        }

        fn io_requirements(&self) -> Vec<IoRequirement> {
            vec![
                IoRequirement::input::<f64>("in", self.input),
                IoRequirement::output::<f64>("out", self.output),
            ]
        }

        fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
            let sample = io.read_typed::<f64>(self.input)?;
            io.write_typed(self.output, sample.value * self.gain)?;
            Ok(())
        }
    }

    /// Writes `value` to a `Float` output every scan; used to show the last
    /// writer in scan order wins.
    struct Constant {
        name: &'static str,
        output: PointId,
        value: f64,
    }

    impl Component for Constant {
        fn name(&self) -> &str {
            self.name
        }

        fn io_requirements(&self) -> Vec<IoRequirement> {
            vec![IoRequirement::output::<f64>("out", self.output)]
        }

        fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
            io.write_typed(self.output, self.value)?;
            Ok(())
        }
    }

    /// A component with a fixed declaration list and a `step` that does
    /// nothing; used to exercise wiring-time checks.
    struct Declared {
        name: &'static str,
        requirements: Vec<IoRequirement>,
    }

    impl Component for Declared {
        fn name(&self) -> &str {
            self.name
        }

        fn io_requirements(&self) -> Vec<IoRequirement> {
            self.requirements.clone()
        }

        fn step(&mut self, _io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
            Ok(())
        }
    }

    fn float(point: u64) -> (PointId, Value) {
        (PointId(point), Value::Float(0.0))
    }

    /// Reads the executor's current sample for `point` off the driver.
    fn driver_value(driver: &StubDriver, point: u64) -> Value {
        driver.read(PointId(point)).unwrap().value
    }

    #[test]
    fn chained_components_propagate_across_consecutive_scans() {
        // Field input 10 -> A(x2) -> out 20 --loopback--> in 30 -> B(+1) ->
        // out 40. The loopback routes at the driver's step boundary, so the
        // chain advances one stage per scan.
        let run = || {
            let driver = StubDriver::new(
                &[float(10), float(20), float(30), float(40)],
                &[(PointId(20), PointId(30))],
            );
            let map: PointMap = [
                (PointId(10), Direction::In, ValueKind::Float),
                (PointId(20), Direction::Out, ValueKind::Float),
                (PointId(30), Direction::In, ValueKind::Float),
                (PointId(40), Direction::Out, ValueKind::Float),
            ]
            .into_iter()
            .collect();
            let mut executor = Executor::new(
                &driver,
                map,
                vec![
                    Box::new(Scale {
                        name: "a",
                        input: PointId(10),
                        output: PointId(20),
                        gain: 2.0,
                    }),
                    Box::new(Scale {
                        name: "b",
                        input: PointId(30),
                        output: PointId(40),
                        gain: 3.0,
                    }),
                ],
            )
            .unwrap();

            driver.write(PointId(10), Value::Float(5.0)).unwrap();
            executor.scan().unwrap();
            // After one scan the chain has only reached point 20.
            assert_eq!(driver_value(&driver, 20), Value::Float(10.0));
            assert_eq!(driver_value(&driver, 40), Value::Float(0.0));

            driver.advance();
            executor.scan().unwrap();
            // The second scan carries the value through component b.
            assert_eq!(driver_value(&driver, 40), Value::Float(30.0));
            (driver_value(&driver, 20), driver_value(&driver, 40))
        };

        // Identical inputs and scans replay identically.
        assert_eq!(run(), run());
    }

    #[test]
    fn scan_order_is_explicit_and_last_writer_wins() {
        let driver = StubDriver::new(&[float(50)], &[]);
        let map: PointMap = [(PointId(50), Direction::Out, ValueKind::Float)]
            .into_iter()
            .collect();
        let ordered = |first: f64, second: f64| {
            Executor::new(
                &driver,
                map.clone(),
                vec![
                    Box::new(Constant {
                        name: "first",
                        output: PointId(50),
                        value: first,
                    }),
                    Box::new(Constant {
                        name: "second",
                        output: PointId(50),
                        value: second,
                    }),
                ],
            )
            .unwrap()
        };

        ordered(1.0, 2.0).scan().unwrap();
        assert_eq!(driver_value(&driver, 50), Value::Float(2.0));
        ordered(2.0, 1.0).scan().unwrap();
        assert_eq!(driver_value(&driver, 50), Value::Float(1.0));
    }

    #[test]
    fn wiring_rejects_type_and_direction_mismatch() {
        let driver = StubDriver::new(&[float(1), float(2)], &[]);
        let map: PointMap = [
            (PointId(1), Direction::In, ValueKind::Float),
            (PointId(2), Direction::Out, ValueKind::Float),
        ]
        .into_iter()
        .collect();

        // Declared Int against a Float map point.
        let error = Executor::new(
            &driver,
            map.clone(),
            vec![Box::new(Declared {
                name: "typed",
                requirements: vec![IoRequirement::input::<i64>("in", PointId(1))],
            })],
        )
        .unwrap_err();
        assert_eq!(
            error,
            WiringError::TypeMismatch {
                component: "typed".to_string(),
                point: PointId(1),
                declared: ValueKind::Int,
                mapped: ValueKind::Float,
            }
        );
        let message = error.to_string();
        assert!(message.contains("\"typed\""), "{message}");
        assert!(message.contains("io point 1"), "{message}");

        // Declared Out against an In map point.
        let error = Executor::new(
            &driver,
            map.clone(),
            vec![Box::new(Declared {
                name: "backwards",
                requirements: vec![IoRequirement::output::<f64>("out", PointId(1))],
            })],
        )
        .unwrap_err();
        assert_eq!(
            error,
            WiringError::DirectionMismatch {
                component: "backwards".to_string(),
                point: PointId(1),
                declared: Direction::Out,
                mapped: Direction::In,
            }
        );
        let message = error.to_string();
        assert!(message.contains("\"backwards\""), "{message}");
        assert!(message.contains("io point 1"), "{message}");

        // A point the map does not serve at all.
        let error = Executor::new(
            &driver,
            map.clone(),
            vec![Box::new(Declared {
                name: "stray",
                requirements: vec![IoRequirement::input::<f64>("in", PointId(9))],
            })],
        )
        .unwrap_err();
        assert_eq!(
            error,
            WiringError::UnknownPoint {
                component: "stray".to_string(),
                point: PointId(9),
            }
        );

        // One component declaring a point twice.
        let error = Executor::new(
            &driver,
            map,
            vec![Box::new(Declared {
                name: "twice",
                requirements: vec![
                    IoRequirement::input::<f64>("a", PointId(1)),
                    IoRequirement::input::<f64>("b", PointId(1)),
                ],
            })],
        )
        .unwrap_err();
        assert_eq!(
            error,
            WiringError::DuplicateDeclaration {
                component: "twice".to_string(),
                point: PointId(1),
            }
        );
    }

    #[test]
    fn scans_produce_tick_numbered_timestamps() {
        // Records (step tick, input sample tick) each scan.
        struct Recorder {
            input: PointId,
            seen: Arc<Mutex<Vec<(Tick, Tick)>>>,
        }
        impl Component for Recorder {
            fn name(&self) -> &str {
                "recorder"
            }
            fn io_requirements(&self) -> Vec<IoRequirement> {
                vec![
                    IoRequirement::input::<f64>("in", self.input),
                    IoRequirement::output::<f64>("out", PointId(20)),
                ]
            }
            fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
                // The typed `Input`/`Output` handles work over `ComponentIo`.
                let sample = Input::<f64, _>::new(io, self.input).read()?;
                self.seen.lock().unwrap().push((tick, sample.tick));
                Output::<f64, _>::new(io, PointId(20)).write(1.0)?;
                Ok(())
            }
        }

        let driver = StubDriver::new(&[float(10), float(20)], &[]);
        let map: PointMap = [
            (PointId(10), Direction::In, ValueKind::Float),
            (PointId(20), Direction::Out, ValueKind::Float),
        ]
        .into_iter()
        .collect();
        let seen = Arc::new(Mutex::new(Vec::new()));
        let mut executor = Executor::new(
            &driver,
            map,
            vec![Box::new(Recorder {
                input: PointId(10),
                seen: Arc::clone(&seen),
            })],
        )
        .unwrap();

        assert_eq!(executor.tick(), Tick::ZERO);
        assert_eq!(executor.run(0).unwrap(), Tick::ZERO);
        executor.run(3).unwrap();
        assert_eq!(executor.tick(), Tick(3));

        // The step tick and the stamped input sample agree per scan.
        let expected: Vec<(Tick, Tick)> =
            [1, 2, 3].into_iter().map(|n| (Tick(n), Tick(n))).collect();
        assert_eq!(*seen.lock().unwrap(), expected);
        assert_eq!(executor.component_statuses()[0].last_tick, Some(Tick(3)));
        // The image keeps the scan's tick-numbered samples.
        assert_eq!(executor.sample(PointId(10)).unwrap().tick, Tick(3));
        assert_eq!(executor.sample(PointId(20)).unwrap().tick, Tick(3));
    }

    #[test]
    fn failed_input_read_degrades_to_bad_quality() {
        let driver = StubDriver::new(&[float(10)], &[]);
        driver.write(PointId(10), Value::Float(7.0)).unwrap();
        let map: PointMap = [(PointId(10), Direction::In, ValueKind::Float)]
            .into_iter()
            .collect();
        let mut executor = Executor::new(
            &driver,
            map,
            vec![Box::new(Declared {
                name: "idle",
                requirements: vec![IoRequirement::input::<f64>("in", PointId(10))],
            })],
        )
        .unwrap();

        executor.scan().unwrap();
        assert_eq!(
            executor.sample(PointId(10)).unwrap().value,
            Value::Float(7.0)
        );

        driver.faults.lock().unwrap().insert(PointId(10));
        executor.scan().unwrap();
        let sample = executor.sample(PointId(10)).unwrap();
        // The last known value is held and marked bad; the scan continues.
        assert_eq!(sample.value, Value::Float(7.0));
        assert_eq!(
            sample.quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
        assert_eq!(sample.tick, Tick(2));
    }

    #[test]
    fn failing_step_is_counted_and_scan_continues() {
        struct Fragile;
        impl Component for Fragile {
            fn name(&self) -> &str {
                "fragile"
            }
            fn io_requirements(&self) -> Vec<IoRequirement> {
                vec![IoRequirement::output::<f64>("out", PointId(20))]
            }
            fn step(&mut self, _io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
                Err("computation failed".into())
            }
        }

        let driver = StubDriver::new(&[float(20)], &[]);
        let map: PointMap = [(PointId(20), Direction::Out, ValueKind::Float)]
            .into_iter()
            .collect();
        let mut executor = Executor::new(&driver, map, vec![Box::new(Fragile)]).unwrap();

        executor.run(2).unwrap();
        let status = &executor.component_statuses()[0];
        assert_eq!(status.name, "fragile");
        assert_eq!(status.step_errors, 2);
        assert_eq!(status.last_tick, None);
        assert_eq!(status.last_error.as_deref(), Some("computation failed"));
        // No output was ever written, so nothing was flushed.
        assert_eq!(executor.tick(), Tick(2));
    }

    #[test]
    fn write_sample_propagates_quality_to_output_image() {
        /// Copies its input through with its quality intact.
        struct Passthrough;
        impl Component for Passthrough {
            fn name(&self) -> &str {
                "passthrough"
            }
            fn io_requirements(&self) -> Vec<IoRequirement> {
                vec![
                    IoRequirement::input::<f64>("in", PointId(10)),
                    IoRequirement::output::<f64>("out", PointId(20)),
                ]
            }
            fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
                let sample = io.read(PointId(10))?;
                io.write_sample(PointId(20), sample)?;
                Ok(())
            }
        }

        let driver = StubDriver::new(&[float(10), float(20)], &[]);
        let map: PointMap = [
            (PointId(10), Direction::In, ValueKind::Float),
            (PointId(20), Direction::Out, ValueKind::Float),
        ]
        .into_iter()
        .collect();
        let mut executor = Executor::new(&driver, map, vec![Box::new(Passthrough)]).unwrap();

        driver.faults.lock().unwrap().insert(PointId(10));
        executor.scan().unwrap();
        let output = executor.sample(PointId(20)).unwrap();
        assert_eq!(
            output.quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
    }

    #[test]
    fn undeclared_and_wrong_direction_access_is_rejected() {
        /// Reaches for points it did not declare, or in the wrong direction.
        struct Grabby;
        impl Component for Grabby {
            fn name(&self) -> &str {
                "grabby"
            }
            fn io_requirements(&self) -> Vec<IoRequirement> {
                vec![
                    IoRequirement::input::<f64>("in", PointId(10)),
                    IoRequirement::output::<f64>("out", PointId(20)),
                ]
            }
            fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
                assert_eq!(
                    io.read(PointId(20)),
                    Err(IoError::UnknownPoint(PointId(20)))
                );
                assert_eq!(
                    io.write(PointId(10), Value::Float(1.0)),
                    Err(IoError::UnknownPoint(PointId(10)))
                );
                assert_eq!(
                    io.read(PointId(99)),
                    Err(IoError::UnknownPoint(PointId(99)))
                );
                Ok(())
            }
        }

        let driver = StubDriver::new(&[float(10), float(20)], &[]);
        let map: PointMap = [
            (PointId(10), Direction::In, ValueKind::Float),
            (PointId(20), Direction::Out, ValueKind::Float),
        ]
        .into_iter()
        .collect();
        let mut executor = Executor::new(&driver, map, vec![Box::new(Grabby)]).unwrap();
        executor.scan().unwrap();
        assert_eq!(executor.component_statuses()[0].step_errors, 0);
    }

    #[test]
    fn snapshot_reports_latest_input_reads_and_output_writes() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let map: PointMap = [
            (PointId(10), Direction::In, ValueKind::Float),
            (PointId(20), Direction::Out, ValueKind::Float),
            (PointId(30), Direction::Out, ValueKind::Float),
        ]
        .into_iter()
        .collect();
        let mut executor = Executor::new(
            &driver,
            map,
            vec![Box::new(Scale {
                name: "a",
                input: PointId(10),
                output: PointId(20),
                gain: 2.0,
            })],
        )
        .unwrap();

        driver.write(PointId(10), Value::Float(5.0)).unwrap();
        executor.scan().unwrap();
        let snapshot = executor.snapshot();

        assert_eq!(snapshot.tick, Tick(1));
        // Every mapped point is reported, ordered by ascending id.
        let [input, output, unwritten] = snapshot.points.as_slice() else {
            panic!("three mapped points");
        };
        assert_eq!(input.point, PointId(10));
        assert_eq!(input.direction, Direction::In);
        assert_eq!(
            input.sample.unwrap(),
            Sample::good(Value::Float(5.0), Tick(1))
        );
        assert_eq!(output.point, PointId(20));
        assert_eq!(output.direction, Direction::Out);
        assert_eq!(
            output.sample.unwrap(),
            Sample::good(Value::Float(10.0), Tick(1))
        );
        // A mapped output no component has written reports no sample.
        assert_eq!(unwritten.point, PointId(30));
        assert_eq!(unwritten.direction, Direction::Out);
        assert_eq!(unwritten.sample, None);

        let [component] = snapshot.components.as_slice() else {
            panic!("one registered component");
        };
        assert_eq!(component.name, "a");
        assert_eq!(component.last_tick, Some(Tick(1)));
        assert_eq!(component.step_errors, 0);
        assert_eq!(component.last_error, None);
    }

    #[test]
    fn snapshot_reports_bad_quality_after_failed_input_read() {
        let driver = StubDriver::new(&[float(10)], &[]);
        driver.write(PointId(10), Value::Float(7.0)).unwrap();
        let map: PointMap = [(PointId(10), Direction::In, ValueKind::Float)]
            .into_iter()
            .collect();
        let mut executor = Executor::new(
            &driver,
            map,
            vec![Box::new(Declared {
                name: "idle",
                requirements: vec![IoRequirement::input::<f64>("in", PointId(10))],
            })],
        )
        .unwrap();

        executor.scan().unwrap();
        driver.faults.lock().unwrap().insert(PointId(10));
        executor.scan().unwrap();

        let sample = executor.snapshot().points[0].sample.unwrap();
        // The held value is marked bad in the executor's tick domain.
        assert_eq!(sample.value, Value::Float(7.0));
        assert_eq!(
            sample.quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
        assert_eq!(sample.tick, Tick(2));
    }

    #[test]
    fn snapshot_counts_step_errors_per_component() {
        struct Fragile;
        impl Component for Fragile {
            fn name(&self) -> &str {
                "fragile"
            }
            fn io_requirements(&self) -> Vec<IoRequirement> {
                vec![IoRequirement::output::<f64>("out", PointId(20))]
            }
            fn step(&mut self, _io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
                Err("computation failed".into())
            }
        }

        let driver = StubDriver::new(&[float(20)], &[]);
        let map: PointMap = [(PointId(20), Direction::Out, ValueKind::Float)]
            .into_iter()
            .collect();
        let mut executor = Executor::new(&driver, map, vec![Box::new(Fragile)]).unwrap();

        executor.run(2).unwrap();
        let component = &executor.snapshot().components[0];
        assert_eq!(component.name, "fragile");
        assert_eq!(component.step_errors, 2);
        assert_eq!(component.last_tick, None);
        assert_eq!(component.last_error.as_deref(), Some("computation failed"));
    }

    #[test]
    fn snapshot_serializes_to_json() {
        let driver = StubDriver::new(&[float(10), float(20)], &[]);
        let map: PointMap = [
            (PointId(10), Direction::In, ValueKind::Float),
            (PointId(20), Direction::Out, ValueKind::Float),
        ]
        .into_iter()
        .collect();
        let mut executor = Executor::new(
            &driver,
            map,
            vec![Box::new(Scale {
                name: "a",
                input: PointId(10),
                output: PointId(20),
                gain: 2.0,
            })],
        )
        .unwrap();
        driver.write(PointId(10), Value::Float(3.0)).unwrap();
        executor.scan().unwrap();

        let json = serde_json::to_string(&executor.snapshot()).unwrap();
        let snapshot: TelemetrySnapshot = serde_json::from_str(&json).unwrap();
        assert_eq!(snapshot, executor.snapshot());
    }

    fn write_value(point: u64, kind: ValueKind, value: Value) -> Command {
        Command::WriteValue {
            point: PointId(point),
            kind,
            value,
        }
    }

    /// Setpoint rig: `Scale` reads `In` point 10 and drives `Out` point 20
    /// at gain 2, plus an unwritten `Out` point 30.
    fn setpoint_rig(driver: &StubDriver) -> Executor<'_> {
        let map: PointMap = [
            (PointId(10), Direction::In, ValueKind::Float),
            (PointId(20), Direction::Out, ValueKind::Float),
            (PointId(30), Direction::Out, ValueKind::Float),
        ]
        .into_iter()
        .collect();
        Executor::new(
            driver,
            map,
            vec![Box::new(Scale {
                name: "a",
                input: PointId(10),
                output: PointId(20),
                gain: 2.0,
            })],
        )
        .unwrap()
    }

    #[test]
    fn setpoint_command_applies_at_next_scan_and_holds() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = setpoint_rig(&driver);
        let command = write_value(10, ValueKind::Float, Value::Float(5.0));

        let receipt = executor.submit_command(command);
        assert_eq!(
            receipt,
            CommandReceipt {
                command,
                outcome: CommandOutcome::Accepted {
                    apply_tick: Tick(1)
                },
            }
        );
        // Queued, not yet applied: the driver still holds the old value.
        assert_eq!(driver_value(&driver, 10), Value::Float(0.0));

        executor.scan().unwrap();
        assert_eq!(driver_value(&driver, 20), Value::Float(10.0));
        assert_eq!(
            executor.receipts().last().unwrap().outcome,
            CommandOutcome::Applied { tick: Tick(1) }
        );

        // The write landed on the field, so the setpoint holds for later
        // scans rather than reverting.
        executor.scan().unwrap();
        assert_eq!(driver_value(&driver, 20), Value::Float(10.0));
        assert_eq!(
            executor.snapshot().points[0].sample.unwrap().value,
            Value::Float(5.0)
        );
    }

    #[test]
    fn invalid_commands_are_rejected_at_submission() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = setpoint_rig(&driver);

        let receipt = executor.submit_command(write_value(99, ValueKind::Float, Value::Float(1.0)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::UnknownPoint { point: PointId(99) }
            }
        );
        assert_eq!(receipt.outcome, executor.receipts().last().unwrap().outcome);

        // The declared kind disagrees with the map's kind.
        let receipt = executor.submit_command(write_value(10, ValueKind::Int, Value::Int(1)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::TypeMismatch {
                    point: PointId(10),
                    expected: ValueKind::Float,
                    found: Value::Int(1),
                }
            }
        );

        // The value's variant disagrees with the declared kind.
        let receipt = executor.submit_command(write_value(10, ValueKind::Float, Value::Bool(true)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::TypeMismatch {
                    point: PointId(10),
                    expected: ValueKind::Float,
                    found: Value::Bool(true),
                }
            }
        );

        // Rejected commands were never queued: the next scan adds no
        // application receipts.
        executor.scan().unwrap();
        assert_eq!(executor.receipts().len(), 3);
        assert!(driver_value(&driver, 10) == Value::Float(0.0));
    }

    #[test]
    fn driver_rejection_is_recorded_at_the_scan_boundary() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = setpoint_rig(&driver);
        driver.faults.lock().unwrap().insert(PointId(30));

        let command = write_value(30, ValueKind::Float, Value::Float(9.0));
        let receipt = executor.submit_command(command);
        // Static checks pass — the fault only surfaces at the driver.
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(1)
            }
        );

        executor.scan().unwrap();
        assert_eq!(
            executor.receipts().last().unwrap().outcome,
            CommandOutcome::Rejected {
                reason: CommandError::DriverRejected {
                    point: PointId(30),
                    error: IoError::Disconnected(PointId(30)),
                }
            }
        );
    }

    #[test]
    fn output_command_without_a_component_writer_persists() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = setpoint_rig(&driver);

        executor.submit_command(write_value(30, ValueKind::Float, Value::Float(7.0)));
        executor.scan().unwrap();

        // No component writes point 30, so the command's value reaches the
        // field and the snapshot.
        assert_eq!(driver_value(&driver, 30), Value::Float(7.0));
        assert_eq!(
            executor.snapshot().points[2].sample.unwrap().value,
            Value::Float(7.0)
        );

        // A component writing the same `Out` point wins inside the scan:
        // it rewrites point 20 from the (still zero) input.
        executor.submit_command(write_value(20, ValueKind::Float, Value::Float(-1.0)));
        executor.scan().unwrap();
        assert_eq!(driver_value(&driver, 20), Value::Float(0.0));
    }

    #[test]
    fn identical_command_runs_produce_identical_receipts() {
        let run = || {
            let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
            let mut executor = setpoint_rig(&driver);
            executor.submit_command(write_value(10, ValueKind::Float, Value::Float(5.0)));
            executor.submit_command(write_value(99, ValueKind::Float, Value::Float(0.0)));
            executor.submit_command(write_value(30, ValueKind::Float, Value::Float(7.0)));
            executor.run(2).unwrap();
            executor.submit_command(write_value(10, ValueKind::Float, Value::Float(8.0)));
            executor.scan().unwrap();
            (
                executor.receipts().to_vec(),
                serde_json::to_string(&executor.snapshot()).unwrap(),
            )
        };

        assert_eq!(run(), run());
        // And the log is the documented one: exactly one receipt per
        // command, in submission order — accepted commands read `Applied`
        // once their scan boundary has passed.
        let (receipts, _) = run();
        let outcomes: Vec<CommandOutcome> =
            receipts.iter().map(|receipt| receipt.outcome).collect();
        assert_eq!(
            outcomes,
            vec![
                CommandOutcome::Applied { tick: Tick(1) },
                CommandOutcome::Rejected {
                    reason: CommandError::UnknownPoint { point: PointId(99) }
                },
                CommandOutcome::Applied { tick: Tick(1) },
                CommandOutcome::Applied { tick: Tick(3) },
            ]
        );
    }

    /// A stateful component: counts its scans into an `Int` output and
    /// carries `count` across a checkpoint.
    struct Counter {
        name: &'static str,
        output: PointId,
        count: i64,
    }

    impl Component for Counter {
        fn name(&self) -> &str {
            self.name
        }

        fn io_requirements(&self) -> Vec<IoRequirement> {
            vec![IoRequirement::output::<i64>("out", self.output)]
        }

        fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
            self.count += 1;
            io.write_typed(self.output, self.count)?;
            Ok(())
        }

        fn capture_state(&self) -> dcs_core::StateMap {
            let mut state = dcs_core::StateMap::new();
            state.insert("count", Value::Int(self.count));
            state
        }

        fn restore_state(
            &mut self,
            state: &dcs_core::StateMap,
        ) -> Result<(), dcs_core::StateError> {
            state.ensure_known_fields(self.name, &["count"])?;
            self.count = state.require_i64(self.name, "count")?;
            Ok(())
        }
    }

    /// Rig for checkpoint tests: `Scale` (stateless) reads In 10 onto Out
    /// 20 at gain 2; `Counter` (stateful) counts scans onto Out 30.
    fn checkpoint_rig(driver: &StubDriver) -> Executor<'_> {
        let map: PointMap = [
            (PointId(10), Direction::In, ValueKind::Float),
            (PointId(20), Direction::Out, ValueKind::Float),
            (PointId(30), Direction::Out, ValueKind::Int),
        ]
        .into_iter()
        .collect();
        Executor::new(
            driver,
            map,
            vec![
                Box::new(Scale {
                    name: "a",
                    input: PointId(10),
                    output: PointId(20),
                    gain: 2.0,
                }),
                Box::new(Counter {
                    name: "count",
                    output: PointId(30),
                    count: 0,
                }),
            ],
        )
        .unwrap()
    }

    fn int(point: u64) -> (PointId, Value) {
        (PointId(point), Value::Int(0))
    }

    #[test]
    fn checkpoint_captures_tick_components_driver_and_outputs() {
        let driver = StubDriver::new(&[float(10), float(20), int(30)], &[]);
        driver.write(PointId(10), Value::Float(5.0)).unwrap();
        let mut executor = checkpoint_rig(&driver);
        executor.run(3).unwrap();

        let checkpoint = executor.checkpoint();
        assert_eq!(checkpoint.tick, Tick(3));
        assert_eq!(checkpoint.components.len(), 2);
        // Stateless components capture an empty map and stay unaffected.
        assert!(checkpoint.components["a"].is_empty());
        assert_eq!(
            checkpoint.components["count"].get("count"),
            Some(Value::Int(3))
        );
        // The stub driver does not implement the contract.
        assert_eq!(checkpoint.driver, None);
        // The last written outputs come from the scan image.
        assert_eq!(
            checkpoint.outputs[&PointId(20)],
            Sample::good(Value::Float(10.0), Tick(3))
        );
        assert_eq!(
            checkpoint.outputs[&PointId(30)],
            Sample::good(Value::Int(3), Tick(3))
        );
    }

    #[test]
    fn restored_executor_continues_the_run_identically() {
        let run = |checkpoint: Option<&Checkpoint>| {
            let driver = StubDriver::new(&[float(10), float(20), int(30)], &[]);
            driver.write(PointId(10), Value::Float(5.0)).unwrap();
            let mut executor = match checkpoint {
                Some(checkpoint) => Executor::restore(
                    &driver,
                    [
                        (PointId(10), Direction::In, ValueKind::Float),
                        (PointId(20), Direction::Out, ValueKind::Float),
                        (PointId(30), Direction::Out, ValueKind::Int),
                    ]
                    .into_iter()
                    .collect(),
                    vec![
                        Box::new(Scale {
                            name: "a",
                            input: PointId(10),
                            output: PointId(20),
                            gain: 2.0,
                        }),
                        Box::new(Counter {
                            name: "count",
                            output: PointId(30),
                            count: 0,
                        }),
                    ],
                    checkpoint,
                )
                .unwrap(),
                None => checkpoint_rig(&driver),
            };
            if checkpoint.is_none() {
                executor.run(3).unwrap();
            }
            let mut samples = Vec::new();
            for _ in 0..2 {
                executor.scan().unwrap();
                samples.push((
                    executor.sample(PointId(10)).unwrap(),
                    executor.sample(PointId(20)).unwrap(),
                    executor.sample(PointId(30)).unwrap(),
                ));
            }
            (executor.tick(), samples)
        };

        // The reference run reaches tick 5 uninterrupted.
        let driver = StubDriver::new(&[float(10), float(20), int(30)], &[]);
        driver.write(PointId(10), Value::Float(5.0)).unwrap();
        let mut original = checkpoint_rig(&driver);
        original.run(3).unwrap();
        let checkpoint = original.checkpoint();

        // The standby driver observes the process itself — the same
        // input value must be supplied to it, as on live hardware.
        assert_eq!(run(Some(&checkpoint)), run(None));
    }

    #[test]
    fn restore_rejects_a_mismatched_component_set() {
        let driver = StubDriver::new(&[float(10), float(20), int(30)], &[]);
        let mut executor = checkpoint_rig(&driver);
        executor.scan().unwrap();
        let checkpoint = executor.checkpoint();

        // The checkpoint names a component the fresh executor lacks.
        let error = Executor::restore(
            &driver,
            [
                (PointId(10), Direction::In, ValueKind::Float),
                (PointId(20), Direction::Out, ValueKind::Float),
                (PointId(30), Direction::Out, ValueKind::Int),
            ]
            .into_iter()
            .collect(),
            vec![Box::new(Scale {
                name: "other",
                input: PointId(10),
                output: PointId(20),
                gain: 2.0,
            })],
            &checkpoint,
        )
        .unwrap_err();
        assert_eq!(
            error,
            RestoreError::UnknownComponent {
                component: "a".to_string()
            }
        );
    }

    #[test]
    fn restore_rejects_incompatible_component_state() {
        let driver = StubDriver::new(&[float(10), float(20), int(30)], &[]);
        let mut executor = checkpoint_rig(&driver);
        executor.scan().unwrap();
        let mut checkpoint = executor.checkpoint();
        checkpoint
            .components
            .get_mut("count")
            .unwrap()
            .insert("count", Value::Float(1.0));

        let error = Executor::restore(
            &driver,
            [
                (PointId(10), Direction::In, ValueKind::Float),
                (PointId(20), Direction::Out, ValueKind::Float),
                (PointId(30), Direction::Out, ValueKind::Int),
            ]
            .into_iter()
            .collect(),
            vec![
                Box::new(Scale {
                    name: "a",
                    input: PointId(10),
                    output: PointId(20),
                    gain: 2.0,
                }),
                Box::new(Counter {
                    name: "count",
                    output: PointId(30),
                    count: 0,
                }),
            ],
            &checkpoint,
        )
        .unwrap_err();
        assert!(matches!(
            error,
            RestoreError::Component(dcs_core::StateError::IncompatibleField { .. })
        ));
    }

    #[test]
    fn restore_reports_driver_state_rejection() {
        /// A driver implementing the contract, so the checkpoint carries
        /// a driver section.
        struct StatefulDriver(StubDriver);
        impl IoDriver for StatefulDriver {
            fn read(&self, point: PointId) -> Result<Sample, IoError> {
                self.0.read(point)
            }
            fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
                self.0.write(point, value)
            }
            fn capture_state(&self) -> Option<dcs_core::StateMap> {
                let mut state = dcs_core::StateMap::new();
                state.insert("tick", Value::Int(1));
                Some(state)
            }
        }

        let active = StatefulDriver(StubDriver::new(&[float(10), float(20), int(30)], &[]));
        let map: PointMap = [
            (PointId(10), Direction::In, ValueKind::Float),
            (PointId(20), Direction::Out, ValueKind::Float),
            (PointId(30), Direction::Out, ValueKind::Int),
        ]
        .into_iter()
        .collect();
        let mut executor = Executor::new(
            &active,
            map.clone(),
            vec![Box::new(Counter {
                name: "count",
                output: PointId(30),
                count: 0,
            })],
        )
        .unwrap();
        executor.scan().unwrap();
        let checkpoint = executor.checkpoint();
        assert!(checkpoint.driver.is_some());

        // The standby's driver does not implement the contract and
        // rejects the captured section by name.
        let standby = StubDriver::new(&[float(10), float(20), int(30)], &[]);
        let error = Executor::restore(
            &standby,
            map,
            vec![Box::new(Counter {
                name: "count",
                output: PointId(30),
                count: 0,
            })],
            &checkpoint,
        )
        .unwrap_err();
        assert!(matches!(
            error,
            RestoreError::Driver(dcs_core::StateError::UnknownField { .. })
        ));
    }

    #[test]
    fn duplicate_component_names_are_rejected_at_wiring() {
        let driver = StubDriver::new(&[float(50)], &[]);
        let map: PointMap = [(PointId(50), Direction::Out, ValueKind::Float)]
            .into_iter()
            .collect();
        let error = Executor::new(
            &driver,
            map,
            vec![
                Box::new(Constant {
                    name: "same",
                    output: PointId(50),
                    value: 1.0,
                }),
                Box::new(Constant {
                    name: "same",
                    output: PointId(50),
                    value: 2.0,
                }),
            ],
        )
        .unwrap_err();
        assert_eq!(
            error,
            WiringError::DuplicateComponent {
                component: "same".to_string()
            }
        );
    }

    #[test]
    fn snapshot_reports_default_descriptor_built_from_declared_io() {
        // `Scale` does not override `describe`: the executor must still
        // report a descriptor derived from its name and declared I/O.
        let driver = StubDriver::new(&[float(10), float(20)], &[]);
        let map: PointMap = [
            (PointId(10), Direction::In, ValueKind::Float),
            (PointId(20), Direction::Out, ValueKind::Float),
        ]
        .into_iter()
        .collect();
        let executor = Executor::new(
            &driver,
            map,
            vec![Box::new(Scale {
                name: "a",
                input: PointId(10),
                output: PointId(20),
                gain: 2.0,
            })],
        )
        .unwrap();

        let snapshot = executor.snapshot();
        let [descriptor] = snapshot.descriptors.as_slice() else {
            panic!("one descriptor per registered component");
        };
        assert_eq!(
            *descriptor,
            ComponentDescriptor {
                name: "a".to_string(),
                kind: std::any::type_name::<Scale>().to_string(),
                label: "a".to_string(),
                ports: vec![
                    PortDescriptor {
                        name: "in".to_string(),
                        direction: Direction::In,
                        kind: ValueKind::Float,
                        role: None,
                    },
                    PortDescriptor {
                        name: "out".to_string(),
                        direction: Direction::Out,
                        kind: ValueKind::Float,
                        role: None,
                    },
                ],
                parameters: Vec::new(),
            }
        );
    }

    #[test]
    fn custom_descriptor_surfaces_in_snapshot() {
        /// A component overriding `describe` with kind, role hints, and
        /// parameter metadata.
        struct Fancy {
            input: PointId,
            output: PointId,
        }
        impl Component for Fancy {
            fn name(&self) -> &str {
                "fancy"
            }
            fn io_requirements(&self) -> Vec<IoRequirement> {
                vec![
                    IoRequirement::input::<f64>("pv", self.input),
                    IoRequirement::output::<f64>("out", self.output),
                ]
            }
            fn step(&mut self, _io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
                Ok(())
            }
            fn describe(&self) -> ComponentDescriptor {
                ComponentDescriptor {
                    name: "fancy".to_string(),
                    kind: "fancy".to_string(),
                    label: "Fancy loop".to_string(),
                    ports: vec![
                        PortDescriptor {
                            name: "pv".to_string(),
                            direction: Direction::In,
                            kind: ValueKind::Float,
                            role: Some(PortRole::ProcessValue),
                        },
                        PortDescriptor {
                            name: "out".to_string(),
                            direction: Direction::Out,
                            kind: ValueKind::Float,
                            role: Some(PortRole::Output),
                        },
                    ],
                    parameters: vec![ParameterDescriptor {
                        name: "gain".to_string(),
                        kind: ValueKind::Float,
                        range: Some(ParameterRange {
                            min: Value::Float(0.0),
                            max: Value::Float(10.0),
                        }),
                    }],
                }
            }
        }

        let driver = StubDriver::new(&[float(10), float(20)], &[]);
        let map: PointMap = [
            (PointId(10), Direction::In, ValueKind::Float),
            (PointId(20), Direction::Out, ValueKind::Float),
        ]
        .into_iter()
        .collect();
        let executor = Executor::new(
            &driver,
            map,
            vec![Box::new(Fancy {
                input: PointId(10),
                output: PointId(20),
            })],
        )
        .unwrap();

        let snapshot = executor.snapshot();
        let [descriptor] = snapshot.descriptors.as_slice() else {
            panic!("one descriptor per registered component");
        };
        assert_eq!(descriptor.kind, "fancy");
        assert_eq!(descriptor.label, "Fancy loop");
        assert_eq!(descriptor.ports[0].role, Some(PortRole::ProcessValue));
        assert_eq!(descriptor.ports[1].role, Some(PortRole::Output));
        assert_eq!(
            descriptor.parameters[0].range,
            Some(ParameterRange {
                min: Value::Float(0.0),
                max: Value::Float(10.0),
            })
        );
    }

    /// Two-chained-`Scale` rig used to show descriptor ordering.
    fn chain_rig(driver: &StubDriver) -> Executor<'_> {
        let map: PointMap = [
            (PointId(10), Direction::In, ValueKind::Float),
            (PointId(20), Direction::Out, ValueKind::Float),
            (PointId(30), Direction::In, ValueKind::Float),
            (PointId(40), Direction::Out, ValueKind::Float),
        ]
        .into_iter()
        .collect();
        Executor::new(
            driver,
            map,
            vec![
                Box::new(Scale {
                    name: "a",
                    input: PointId(10),
                    output: PointId(20),
                    gain: 2.0,
                }),
                Box::new(Scale {
                    name: "b",
                    input: PointId(30),
                    output: PointId(40),
                    gain: 3.0,
                }),
            ],
        )
        .unwrap()
    }

    #[test]
    fn descriptors_follow_scan_order_deterministically() {
        let driver_a = StubDriver::new(&[float(10), float(20), float(30), float(40)], &[]);
        let driver_b = StubDriver::new(&[float(10), float(20), float(30), float(40)], &[]);
        let mut run_a = chain_rig(&driver_a);
        let mut run_b = chain_rig(&driver_b);
        run_a.scan().unwrap();
        run_b.scan().unwrap();

        let snapshot_a = run_a.snapshot();
        let snapshot_b = run_b.snapshot();

        // Descriptors align 1:1 with the diagnostics, in scan order.
        let names: Vec<&str> = snapshot_a
            .descriptors
            .iter()
            .map(|descriptor| descriptor.name.as_str())
            .collect();
        assert_eq!(names, ["a", "b"]);
        assert_eq!(snapshot_a.descriptors.len(), snapshot_a.components.len());
        for (descriptor, diagnostics) in snapshot_a
            .descriptors
            .iter()
            .zip(snapshot_a.components.iter())
        {
            assert_eq!(descriptor.name, diagnostics.name);
        }

        // Equivalent runs produce identical descriptors — and identical
        // serialized snapshots.
        assert_eq!(snapshot_a.descriptors, snapshot_b.descriptors);
        assert_eq!(
            serde_json::to_string(&snapshot_a).unwrap(),
            serde_json::to_string(&snapshot_b).unwrap()
        );
    }

    /// A map with a held internal `In` point (an operator setpoint) and an
    /// internal `Out` point (a monitored component write).
    fn internal_map() -> PointMap {
        PointMap::new()
            .with_internal(
                PointId(10),
                Direction::In,
                ValueKind::Float,
                Value::Float(2.5),
            )
            .with_internal(
                PointId(20),
                Direction::Out,
                ValueKind::Float,
                Value::Float(0.0),
            )
    }

    /// `Scale` reads the internal `In` point onto the internal `Out` one.
    fn internal_rig(driver: &StubDriver) -> Executor<'_> {
        Executor::new(
            driver,
            internal_map(),
            vec![Box::new(Scale {
                name: "a",
                input: PointId(10),
                output: PointId(20),
                gain: 2.0,
            })],
        )
        .unwrap()
    }

    #[test]
    fn internal_points_are_served_from_the_image() {
        // The driver serves no points at all: every value here is
        // image-carried.
        let driver = StubDriver::new(&[], &[]);
        let mut executor = internal_rig(&driver);

        // The declared initial is seeded at wiring, before any scan.
        assert_eq!(
            executor.sample(PointId(10)),
            Some(Sample::good(Value::Float(2.5), Tick::ZERO))
        );
        executor.scan().unwrap();
        // The component read the held initial and its write landed on the
        // image-carried `Out` point — recorded for monitoring.
        assert_eq!(
            executor.sample(PointId(20)),
            Some(Sample::good(Value::Float(5.0), Tick(1)))
        );

        // A command writes the internal `In` point at the scan boundary.
        let receipt = executor.submit_command(Command::WriteValue {
            point: PointId(10),
            kind: ValueKind::Float,
            value: Value::Float(7.0),
        });
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(2)
            }
        );
        executor.scan().unwrap();
        assert_eq!(
            executor.receipts()[0].outcome,
            CommandOutcome::Applied { tick: Tick(2) }
        );
        assert_eq!(
            executor.sample(PointId(10)),
            Some(Sample::good(Value::Float(7.0), Tick(2)))
        );
        // The component observed the commanded value the same scan.
        assert_eq!(
            executor.sample(PointId(20)),
            Some(Sample::good(Value::Float(14.0), Tick(2)))
        );

        // The held value survives later scans until the next command.
        executor.scan().unwrap();
        assert_eq!(
            executor.sample(PointId(10)).unwrap().value,
            Value::Float(7.0)
        );

        // Internal points appear in the telemetry snapshot like field
        // points.
        let snapshot = executor.snapshot();
        assert_eq!(snapshot.points.len(), 2);
        assert_eq!(
            snapshot.points[0],
            PointTelemetry {
                point: PointId(10),
                direction: Direction::In,
                sample: Some(Sample::good(Value::Float(7.0), Tick(2))),
            }
        );
    }

    #[test]
    fn internal_link_delivers_a_write_one_scan_later() {
        // The producer writes internal `Out` 20 during the step phase;
        // the link routes it onto internal `In` 30 at the next scan's
        // input phase, where the consumer's step observes it — the same
        // boundary a field loopback crosses.
        let driver = StubDriver::new(&[float(40)], &[]);
        let map = PointMap::new()
            .with_internal(
                PointId(20),
                Direction::Out,
                ValueKind::Float,
                Value::Float(0.0),
            )
            .with_internal(
                PointId(30),
                Direction::In,
                ValueKind::Float,
                Value::Float(-1.0),
            )
            .with_internal_link(PointId(20), PointId(30))
            .with_point(PointId(40), Direction::Out, ValueKind::Float);
        let mut executor = Executor::new(
            &driver,
            map,
            vec![
                Box::new(Constant {
                    name: "producer",
                    output: PointId(20),
                    value: 9.0,
                }),
                Box::new(Scale {
                    name: "consumer",
                    input: PointId(30),
                    output: PointId(40),
                    gain: 1.0,
                }),
            ],
        )
        .unwrap();

        executor.scan().unwrap();
        // Scan 1's input phase routed the `Out` point's seeded initial;
        // the producer's write lands on the `In` point at scan 2.
        assert_eq!(
            executor.sample(PointId(30)),
            Some(Sample::good(Value::Float(0.0), Tick(1)))
        );
        assert_eq!(driver_value(&driver, 40), Value::Float(0.0));

        executor.scan().unwrap();
        assert_eq!(
            executor.sample(PointId(30)),
            Some(Sample::good(Value::Float(9.0), Tick(2)))
        );
        assert_eq!(driver_value(&driver, 40), Value::Float(9.0));
    }

    #[test]
    fn malformed_internal_links_fail_wiring() {
        let driver = StubDriver::new(&[float(1)], &[]);
        let internal = || {
            PointMap::new()
                .with_point(PointId(1), Direction::In, ValueKind::Float)
                .with_internal(
                    PointId(20),
                    Direction::Out,
                    ValueKind::Float,
                    Value::Float(0.0),
                )
                .with_internal(
                    PointId(30),
                    Direction::In,
                    ValueKind::Float,
                    Value::Float(0.0),
                )
        };
        let wire = |map: PointMap| Executor::new(&driver, map, Vec::new());

        // The `output` end must be a mapped internal `Out` point: field
        // and unmapped ends are rejected alike.
        for output in [PointId(1), PointId(99)] {
            assert_eq!(
                wire(internal().with_internal_link(output, PointId(30))).unwrap_err(),
                WiringError::InvalidLink {
                    output,
                    input: PointId(30),
                    detail: LinkError::Output,
                }
            );
        }
        // The `input` end must be a mapped internal `In` point.
        assert_eq!(
            wire(internal().with_internal_link(PointId(20), PointId(1))).unwrap_err(),
            WiringError::InvalidLink {
                output: PointId(20),
                input: PointId(1),
                detail: LinkError::Input,
            }
        );
        // The ends must carry the same value kind.
        assert_eq!(
            wire(
                internal()
                    .with_internal(PointId(31), Direction::In, ValueKind::Int, Value::Int(0))
                    .with_internal_link(PointId(20), PointId(31))
            )
            .unwrap_err(),
            WiringError::InvalidLink {
                output: PointId(20),
                input: PointId(31),
                detail: LinkError::KindMismatch,
            }
        );
        // An `In` point may be driven by only one link.
        assert_eq!(
            wire(
                internal()
                    .with_internal_link(PointId(20), PointId(30))
                    .with_internal_link(PointId(20), PointId(30))
            )
            .unwrap_err(),
            WiringError::InvalidLink {
                output: PointId(20),
                input: PointId(30),
                detail: LinkError::Conflict,
            }
        );
    }

    #[test]
    fn checkpoint_carries_internal_in_samples() {
        let driver = StubDriver::new(&[], &[]);
        let mut executor = internal_rig(&driver);
        executor.submit_command(Command::WriteValue {
            point: PointId(10),
            kind: ValueKind::Float,
            value: Value::Float(7.0),
        });
        executor.scan().unwrap();

        let checkpoint = executor.checkpoint();
        assert_eq!(
            checkpoint.internal[&PointId(10)],
            Sample::good(Value::Float(7.0), Tick(1))
        );
        assert_eq!(
            checkpoint.outputs[&PointId(20)],
            Sample::good(Value::Float(14.0), Tick(1))
        );

        // A restored executor resumes with the commanded value rather
        // than the declared initial — operator state transfers.
        let driver = StubDriver::new(&[], &[]);
        let restored = Executor::restore(
            &driver,
            internal_map(),
            vec![Box::new(Scale {
                name: "a",
                input: PointId(10),
                output: PointId(20),
                gain: 2.0,
            })],
            &checkpoint,
        )
        .unwrap();
        assert_eq!(
            restored.sample(PointId(10)),
            Some(Sample::good(Value::Float(7.0), Tick(1)))
        );
    }
}
