//! The cyclic executor: wiring verification, the scan image, and the
//! virtual tick.
//!
//! [`Executor::new`] checks every component's declared I/O against the
//! driver's [`PointMap`] before anything runs; [`Executor::scan`] and
//! [`Executor::run`] then advance a virtual [`Tick`] and cycle read → step
//! → write deterministically.

use crate::component::{Component, ComponentIo, IoRequirement};
use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, ComponentDiagnostics, Direction,
    IoDriver, IoError, PointId, PointTelemetry, Quality, QualityReason, Sample, TelemetrySnapshot,
    Tick, Value, ValueKind,
};
use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap, VecDeque};
use std::fmt;

/// How the controller may use one point the driver serves.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PointSpec {
    /// `In` points are read into the scan image; `Out` points are written
    /// from the image to the driver.
    pub direction: Direction,
    /// The point's declared value kind.
    pub kind: ValueKind,
}

/// The driver's point map: which logical points the driver serves and how
/// the controller may use them.
///
/// The map is the resolved, driver-side form of the plant model's I/O
/// mapping — the caller that built the driver supplies it, and the executor
/// treats it as the authority every component's declared I/O is checked
/// against at wiring time.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct PointMap {
    points: BTreeMap<PointId, PointSpec>,
}

impl PointMap {
    /// An empty map.
    pub fn new() -> Self {
        Self::default()
    }

    /// Adds `point` with the given direction and value kind; a repeated id
    /// replaces the earlier spec.
    pub fn with_point(mut self, point: PointId, direction: Direction, kind: ValueKind) -> Self {
        self.points.insert(point, PointSpec { direction, kind });
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

/// Why an executor refuses to run: a component's declared I/O does not
/// match the driver's point map. Every variant names the component and the
/// point.
#[derive(Debug, Clone, PartialEq)]
pub enum WiringError {
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
}

impl fmt::Display for WiringError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
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
///    take effect, each producing a receipt;
/// 3. reads every `In` point in the map into the scan image, stamping the
///    new tick — a failed read keeps the last known value marked
///    [`Quality::Bad`] rather than aborting the scan;
/// 4. steps the components in scan order, each seeing a [`ComponentIo`]
///    scoped to its declared points — a failing step is recorded and the
///    scan continues;
/// 5. writes the image's `Out` points to the driver — points a component
///    never wrote keep their last output, so a failed step holds outputs.
///
/// Applying commands before the input read gives a command to an `In`
/// point setpoint semantics: the same scan's input phase observes the
/// written value, and it holds for later scans until the field-side value
/// changes again. A command to an `Out` point lands in the image where a
/// component write later in the same scan may still override it —
/// components win, keeping control logic authoritative inside a scan.
///
/// Nothing reads a wall clock: identical driver behavior over identical
/// scans produces identical samples on every host.
pub struct Executor<'d> {
    driver: &'d (dyn IoDriver + Sync),
    map: PointMap,
    components: Vec<Entry>,
    image: RefCell<HashMap<PointId, Sample>>,
    pending_commands: VecDeque<Command>,
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
        for component in components {
            let component_name = || component.name().to_string();
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
        Ok(Self {
            driver,
            map,
            components: entries,
            image: RefCell::new(HashMap::new()),
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
        }
    }

    /// Queues `command` for application at the start of the next scan and
    /// returns its first receipt.
    ///
    /// Submission validates the command against the point map — the point
    /// must be served, and the declared kind must match both the map's
    /// kind and the supplied value's variant — so an invalid command is
    /// [`CommandOutcome::Rejected`] immediately and never queued. An
    /// [`CommandOutcome::Accepted`] command applies at the head of the
    /// next [`scan`](Executor::scan), before the input-read phase, where
    /// it produces a second receipt: [`CommandOutcome::Applied`], or
    /// `Rejected` with [`CommandError::DriverRejected`] when the field
    /// driver refuses the write. Every receipt is appended to the
    /// [`receipts`](Executor::receipts) log in order.
    pub fn submit_command(&mut self, command: Command) -> CommandReceipt {
        let outcome = match self.check_command(command) {
            Err(reason) => CommandOutcome::Rejected { reason },
            Ok(_) => {
                self.pending_commands.push_back(command);
                CommandOutcome::Accepted {
                    apply_tick: Tick(self.tick.0 + 1),
                }
            }
        };
        let receipt = CommandReceipt { command, outcome };
        self.receipts.push(receipt);
        receipt
    }

    /// The receipt log: every submission and application receipt, in the
    /// order they were produced.
    ///
    /// An accepted command appears twice — `Accepted` at submission and
    /// `Applied` or `Rejected` at the scan boundary — so the log is the
    /// audit trail a monitoring consumer reads alongside the snapshot.
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

    /// Applies every queued command in submission order and records each
    /// one's receipt. Runs at the head of the scan — before the input
    /// read — so a write to an `In` point is observed by this scan's
    /// input phase, while a write to an `Out` point enters the image
    /// where the step phase may still override it.
    fn apply_commands(&mut self, tick: Tick) {
        while let Some(command) = self.pending_commands.pop_front() {
            let outcome = match self.check_command(command) {
                Err(reason) => CommandOutcome::Rejected { reason },
                Ok((point, value)) => match self.driver.write(point, value) {
                    Err(error) => CommandOutcome::Rejected {
                        reason: CommandError::DriverRejected { point, error },
                    },
                    Ok(()) => {
                        self.image
                            .borrow_mut()
                            .insert(point, Sample::good(value, tick));
                        CommandOutcome::Applied { tick }
                    }
                },
            };
            self.receipts.push(CommandReceipt { command, outcome });
        }
    }

    /// Reads every `In` point in the map into the image, stamping `tick`.
    /// A failed read keeps the last known value — a neutral one if none —
    /// marked `Bad`, so a field fault degrades inputs instead of stopping
    /// the controller.
    fn read_inputs(&mut self, tick: Tick) {
        let mut image = self.image.borrow_mut();
        for (point, spec) in self.map.iter() {
            if spec.direction != Direction::In {
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

    /// Writes every `Out` point the image holds to the driver. Points a
    /// component never wrote keep no image entry and are left untouched.
    fn write_outputs(&mut self) -> Result<(), ScanError> {
        let image = self.image.borrow();
        for (point, spec) in self.map.iter() {
            if spec.direction != Direction::Out {
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
    use dcs_core::{Input, Output};
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
        // And the sequence is the documented one: each accepted command
        // receipts Accepted at submission then Applied at the boundary;
        // the unknown point is Rejected once at submission.
        let (receipts, _) = run();
        let outcomes: Vec<CommandOutcome> =
            receipts.iter().map(|receipt| receipt.outcome).collect();
        assert_eq!(
            outcomes,
            vec![
                CommandOutcome::Accepted {
                    apply_tick: Tick(1)
                },
                CommandOutcome::Rejected {
                    reason: CommandError::UnknownPoint { point: PointId(99) }
                },
                CommandOutcome::Accepted {
                    apply_tick: Tick(1)
                },
                CommandOutcome::Applied { tick: Tick(1) },
                CommandOutcome::Applied { tick: Tick(1) },
                CommandOutcome::Accepted {
                    apply_tick: Tick(3)
                },
                CommandOutcome::Applied { tick: Tick(3) },
            ]
        );
    }
}
