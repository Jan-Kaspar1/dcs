//! The cyclic executor: wiring verification, the scan image, and the
//! virtual tick.
//!
//! [`Executor::new`] checks every component's declared I/O against the
//! driver's [`PointMap`] before anything runs; [`Executor::scan`] and
//! [`Executor::run`] then advance a virtual [`Tick`] and cycle read → step
//! → write deterministically.

use crate::checkpoint::{
    CHECKPOINT_FORMAT_VERSION, Checkpoint, CommandAdmissionCounts, RestoreError,
    SUPPORTED_FORMAT_VERSIONS,
};
use crate::component::{Component, ComponentIo, IoRequirement};
use crate::revision::CarryoverError;
use dcs_core::{
    CarriedPoint, CarryoverReport, Command, CommandAvailability, CommandError, CommandOutcome,
    CommandQueueDiagnostics, CommandReceipt, CommandVerdict, ComponentCommands,
    ComponentDiagnostics, ComponentParameters, CyclicIoDriver, Direction, DroppedElement,
    EmittedEvent, ForcedPoint, IoDriver, IoError, IoFault, IoHealth, ModelFingerprint, PointId,
    PointTelemetry, Quality, QualityReason, RevertedParameter, Sample, StateMap, TelemetrySnapshot,
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
    /// Whether the point accepts operator `WriteValue` commands — the
    /// `writable` flag a model `io_point` declaration carries through
    /// assembly. Only `In` points honor the mark: the command path
    /// refuses every `Out` point with
    /// [`CommandError::NotWritable`](dcs_core::CommandError::NotWritable)
    /// regardless.
    pub writable: bool,
    /// The point's declared freshness budget — the `stale_after_ticks` a
    /// model `io_point` declaration carries through assembly.
    /// `Some(budget)` on a field `In` point asks the input phase to land
    /// the image sample as
    /// [`Quality::Uncertain`]`(`[`QualityReason::Stale`]`)` when the
    /// driver-returned sample has not changed in more than `budget`
    /// scan ticks; `None` disables the check, and the budget is
    /// inert on internal points (never driver-read) and `Out` points
    /// (never read).
    pub stale_after_ticks: Option<u64>,
    /// Whether the point's observed value transitions join the durable
    /// journal — the `journaled` flag a model `io_point` declaration
    /// carries through assembly. The monitor's recorder diffs declared
    /// points' image values scan over scan and appends a `PointChanged`
    /// entry at the producing scan's tick; undeclared points journal no
    /// value transitions.
    pub journaled: bool,
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
                writable: false,
                stale_after_ticks: None,
                journaled: false,
            },
        );
        self
    }

    /// Adds `point` with the fully-formed `spec` — the escape hatch for
    /// spec fields the narrow constructors do not take, like a field
    /// `In` point's `stale_after_ticks` freshness budget. A repeated id
    /// replaces the earlier spec.
    pub fn with_spec(mut self, point: PointId, spec: PointSpec) -> Self {
        self.points.insert(point, spec);
        self
    }

    /// Adds `point` as a driver-served field point marked writable — the
    /// flag a model `io_point` declaration carries. A command write to a
    /// writable `In` point is forwarded to the driver at the scan
    /// boundary as documented operator substitution of the input image;
    /// the mark is inert on an `Out` point, whose writes the command
    /// path refuses outright. A repeated id replaces the earlier spec.
    pub fn with_writable_point(
        mut self,
        point: PointId,
        direction: Direction,
        kind: ValueKind,
    ) -> Self {
        self.points.insert(
            point,
            PointSpec {
                direction,
                kind,
                internal: None,
                writable: true,
                stale_after_ticks: None,
                journaled: false,
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
                writable: false,
                stale_after_ticks: None,
                journaled: false,
            },
        );
        self
    }

    /// Adds `point` as an image-carried internal point holding `initial`,
    /// marked writable — the common operator-value target: a command
    /// write lands in the image at the scan boundary and components
    /// observe it in that scan. As for
    /// [`with_writable_point`](Self::with_writable_point), the mark is
    /// inert on an `Out` point. A repeated id replaces the earlier spec.
    pub fn with_writable_internal(
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
                writable: true,
                stale_after_ticks: None,
                journaled: false,
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

/// One budgeted field `In` point's freshness evidence: the driver
/// sample last read and the scan tick at which that observation last
/// changed — the run-domain record the `stale_after_ticks` budget
/// measures, so a driver stamping in its own tick domain still yields
/// a lag inside the run's.
#[derive(Debug, Clone, Copy)]
struct Freshness {
    /// The sample the last successful read returned — the change
    /// marker: a fresh acquisition re-stamps it, so an unchanged
    /// report is held data aging under the budget.
    observed: Sample,
    /// The scan tick `observed` last changed at — or, on the first
    /// observation, the earlier of that tick and the driver's own
    /// stamp: a stamp already behind the scan tick is aged evidence
    /// the run trusts only as far as the stamp claims, while a stamp
    /// in a domain running ahead of the run seeds at the observation
    /// itself — the freshest thing the run has seen.
    since: Tick,
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
    /// The component's parameter report captured at construction — the
    /// revision's declared defaults as the fresh instance stands before
    /// any scan, command, or adopted receipt could tune them. The
    /// reverted-tuning itemization diffs a crossing checkpoint's
    /// declared parameters against this baseline, not the live report:
    /// an `Accepted` receipt the checkpoint adopted and re-settled here,
    /// or a tune aimed at this run directly, must not hide what the old
    /// run's tuning reverted *from*.
    parameter_defaults: StateMap,
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
        IoError::Disconnected(_) | IoError::Timeout(_) | IoError::Fenced(_) => {
            Quality::Bad(QualityReason::CommunicationFault)
        }
        IoError::UnknownPoint(_) | IoError::TypeMismatch { .. } => {
            Quality::Bad(QualityReason::ConfigurationFault)
        }
    }
}

/// Whether a checkpointed parameter value and the revision's declared
/// default stand equal for the reverted-tuning itemization — exact
/// match per kind, `NaN` agreeing only with `NaN` like the divergence
/// rule's comparison: a checkpointed `NaN` against a declared `NaN` is
/// no reversion.
fn parameter_value_stands(checkpointed: Value, declared: Value) -> bool {
    checkpointed == declared
        || matches!(
            (checkpointed, declared),
            (Value::Float(checkpointed), Value::Float(declared))
                if checkpointed.is_nan() && declared.is_nan()
        )
}

/// A command resolved past static validation: what
/// [`Executor::apply_commands`] carries out at the scan boundary.
enum Resolved {
    /// `WriteValue` on a mapped point of the declared kind.
    Write { point: PointId, value: Value },
    /// `SetParameter` on the component at this scan-order index.
    Parameter {
        component: usize,
        name: String,
        value: Value,
    },
    /// `ForcePoint` on a mapped writable `In` point of the declared kind.
    Force { point: PointId, value: Value },
    /// `UnforcePoint` on a mapped writable `In` point.
    Unforce { point: PointId },
    /// `Invoke` of the declared command `command` on the component at
    /// this scan-order index, carrying the validated argument map.
    Invoke {
        component: usize,
        command: String,
        arguments: BTreeMap<String, Value>,
    },
}

/// The rollback record for one effect a command boundary staged: the
/// pre-boundary state [`Executor::supersede_commands`] returns when the
/// boundary's run turns out superseded. Every state a command mutates
/// is run state the checkpoints keep carrying — the image's internal
/// `In` samples, the force set, a component's captured state — so a
/// `Rejected`/`Superseded` receipt is honest only while the abandoned
/// run also unwrote the effect: without the rollback the mutation rode
/// the demoted peer's quiesced checkpoints into the tracking successor
/// while the journal claimed it never landed.
#[derive(Debug)]
enum BoundaryUndo {
    /// An internal `In` point's image sample as the boundary found it —
    /// staged by a `WriteValue` or by the force pair's image re-stamps.
    Image {
        point: PointId,
        prior: Option<Sample>,
    },
    /// A point's `forces` entry as the boundary found it — `None` where
    /// no force stood — staged by `ForcePoint`/`UnforcePoint`.
    Force {
        point: PointId,
        prior: Option<Value>,
    },
    /// A field `In` point's last-observed sample as the boundary's
    /// image held it, for a `WriteValue` the driver accepted: the
    /// compensating write-back a superseded boundary attempts —
    /// best-effort, since a field the claim arbitration fenced may
    /// refuse it, leaving the plant's own record to speak.
    FieldWrite {
        point: PointId,
        prior: Option<Sample>,
    },
    /// The component at this scan-order index, captured before the
    /// boundary's first `SetParameter`/`Invoke` touched it — the
    /// checkpoint vocabulary's own state, restored through the same
    /// [`restore_state`](Component::restore_state) contract a tracking
    /// apply relies on.
    Component { index: usize, state: StateMap },
}

/// The default bound on the pending-command queue — how many accepted
/// commands may wait for their scan boundary before
/// [`Executor::submit_command`] refuses further admissions with
/// [`CommandError::QueueFull`](dcs_core::CommandError::QueueFull).
///
/// The queue drains at every scan boundary, so the bound only has to
/// cover one scan period's ingress: 64 is roomy for operator and tooling
/// bursts inside that window while staying a hard bound a flooded
/// command path cannot pass — the bounded command-ingress half of the
/// controller-owns-execution decision. Declare a different bound at
/// construction through
/// [`Executor::with_command_queue_capacity`].
pub const DEFAULT_COMMAND_QUEUE_CAPACITY: usize = 64;

/// The default bound on the retained command receipt log — how many of
/// the most recent receipts the executor keeps serving once settled
/// receipts ahead of them have evicted.
///
/// The log is the run's command audit, and like every other served
/// stream — history rings, the journal, the publication window — it is
/// bounded so a long-lived run's checkpoint pulls, state-file writes,
/// and `GET /receipts` answers stay flat in lifetime command count.
/// Eviction is oldest-settled-first: a receipt still
/// [`CommandOutcome::Accepted`] — queued for a boundary it has not met
/// — is never evicted, so the log may exceed the bound only by the
/// pending depth it is protecting. Evictions are visible rather than
/// silent: the snapshot's `command_queue.attempts` counts every
/// lifetime submission, so `attempts - receipts().len()` is the count
/// of evicted receipts and [`Executor::receipt_base`] reports the
/// absolute index the retained log starts at — the same seq-gap
/// convention the other bounded streams use.
///
/// 1024 matches the monitor's history and journal defaults: a roomy
/// audit tail at ~116 bytes a receipt while staying a hard bound.
/// Declare a different bound at construction through
/// [`Executor::with_receipt_log_capacity`].
pub const DEFAULT_RECEIPT_LOG_CAPACITY: usize = 1024;

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
/// 3. runs the cyclic exchange when the driver implements the
///    [`CyclicIoDriver`](dcs_core::CyclicIoDriver) contract — detected at
///    wiring through [`IoDriver::cyclic`](dcs_core::IoDriver::cyclic) —
///    one call per scan at the read boundary, publishing the output
///    image the last write phase (and this scan's applied command
///    writes) staged and latching the returned input image atomically.
///    A failed exchange is counted once under `failed_exchanges` and
///    never aborts the scan — the driver's held image still answers the
///    reads that follow. Non-cyclic drivers skip the phase entirely;
/// 4. refreshes the image's `In` points: every field `In` point is read
///    from the driver — the latched image under the cyclic contract —
///    stamping the new tick — a failed read keeps the last known value
///    marked [`Quality::Bad`] rather than aborting the scan, and a
///    `stale_after_ticks` budget on the point merges
///    [`Quality::Uncertain`]`(`[`QualityReason::Stale`]`)` onto a sample
///    whose driver report has not changed within the budget — the lag
///    measured in scan ticks, so a driver stamping in a foreign tick
///    domain still ages held data and releases fresh data correctly;
///    over a cyclic driver the stamp is the producing exchange's
///    acquisition tick, so the budget measures exchange freshness —
///    and every internal link routes its `Out` point's image
///    sample onto its `In` point, so a port-to-port carrier delivers the
///    value one scan after it was written;
/// 5. steps the components in scan order, each seeing a [`ComponentIo`]
///    scoped to its declared points — a failing step is recorded and the
///    scan continues;
/// 6. probes each component's declared
///    [`KindDeclared`](dcs_core::CommandAvailability::KindDeclared)
///    commands through [`command_refusal`](Component::command_refusal) —
///    once per declared command, on the post-step state — refreshing the
///    `command_verdicts` section the next
///    [`snapshot`](Executor::snapshot) carries. The verdicts are
///    advisory: dispatch stays the receipted path's authority, so a
///    probe answer never refuses, applies, or alters a command;
/// 7. writes the image's field `Out` points to the driver — points a
///    component never wrote keep their last output, so a failed step
///    holds outputs, and a failed write is counted and attributed like
///    a failed read rather than aborting the scan: a field outage must
///    degrade the run's telemetry, not end the run that reports it.
///    Under the cyclic contract each write stages the
///    pending output image, publishing on the next scan's exchange —
///    the contract's one-scan actuation delay.
///
/// The driver-boundary failures of phases 3, 4, and 7 are also counted
/// into
/// the snapshot's [`IoHealth`](dcs_core::IoHealth) section: each failed
/// read, write, or exchange increments its named counter and the
/// consecutive-failure streak — which any successful boundary operation
/// resets — and is attributed to its tick and point as the section's
/// `last_error`. The command path's own driver rejections instead settle
/// their receipts as [`CommandError::DriverRejected`], and a driver's
/// volunteered [`IoDriver::diagnostics`] rides the same section, so the
/// snapshot separates link-level degradation from per-point faults — a
/// cyclic driver's exchange counters included.
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
/// The point map marks which points accept operator writes — the
/// `writable` flag of the model's `io_point` declarations — and the
/// command surface is writable `In` points only: a `WriteValue` naming
/// an unmarked point or any `Out` point is refused at submission with
/// [`CommandError::NotWritable`]. The recorded rule for `Out` points is
/// rejection: control logic stays authoritative over outputs inside a
/// scan, so operator influence on an output is engineered through
/// components and their writable `In` inputs, never a raw point write.
///
/// Applying commands before the input read gives a command to a writable
/// `In` point setpoint semantics: a field point's write is forwarded to
/// the driver — documented operator substitution of the input image —
/// and the same scan's input phase reads it back, holding for later
/// scans until the field side asserts a different value; an internal
/// `In` point's write lands in the image directly and holds until the
/// next command.
///
/// A [`Command::SetParameter`] rides the same boundary: it addresses a
/// component by the name its descriptor and diagnostics report, is
/// validated at submission against the component's declared
/// [`ParameterDescriptor`](dcs_core::ParameterDescriptor)s — existence,
/// value kind, and declared range — and lands on the component's
/// [`apply_parameter`](Component::apply_parameter) hook at the head of
/// the next scan, where a component-side refusal turns the receipt
/// `Rejected` without changing anything.
///
/// A [`Command::Invoke`] rides the same boundary and the same bounded
/// queue: submission validates it against the component's declared
/// [`CommandDecl`](dcs_core::CommandDecl)s — the component must exist,
/// the command must be declared, and every supplied argument must carry
/// its declared kind — and the applying scan dispatches it to the
/// component's [`invoke_command`](Component::invoke_command) hook, where
/// the declared availability predicate or a kind invariant settles the
/// receipt `Rejected` with the declared refusal reason. The sibling
/// read side is the [`command_refusal`](Component::command_refusal)
/// probe: the scan's step-end evaluation publishes each declared
/// `KindDeclared` command's standing verdict in the snapshot's
/// `command_verdicts` section — advisory reporting only, with the
/// receipted path remaining the sole authority over what applies.
///
/// A component's declared [`EventDecl`](dcs_core::EventDecl) surface is
/// the sibling output side: [`drain_events`](Component::drain_events)
/// empties each component's emitted events after its `step` — a step's
/// failure does not strand them — and the scan's emitted record is
/// readable through [`emitted_events`](Executor::emitted_events) for the
/// monitor's journal pass, which stamps each entry with the producing
/// scan's tick.
///
/// Ingress is bounded — the controller-owns-execution decision's
/// command half: the pending queue holds at most `command_capacity`
/// accepted commands (default [`DEFAULT_COMMAND_QUEUE_CAPACITY`],
/// declared at construction through
/// [`with_command_queue_capacity`](Executor::with_command_queue_capacity)),
/// and a validated submission past the bound is refused at submission
/// with a [`CommandError::QueueFull`] receipt — named, receipted, and
/// queued as nothing — rather than piling up until the next boundary
/// drains. The queue's admission metrics ride
/// [`snapshot`](Executor::snapshot)'s `command_queue` section.
///
/// The receipt log the submissions produce is bounded too — retention,
/// not admission: past `receipt_capacity` (default
/// [`DEFAULT_RECEIPT_LOG_CAPACITY`], declared through
/// [`with_receipt_log_capacity`](Executor::with_receipt_log_capacity))
/// the leading settled entries evict oldest-first while a receipt
/// still `Accepted` — pending command state — never evicts. The run's
/// audit thereby stays flat in lifetime command count on every hot
/// path it rides: the checkpoint a standby pulls each cycle, the
/// `--state-file` write, and `GET /receipts`. Eviction stays visible:
/// `attempts` counts every lifetime submission, so the gap between it
/// and the retained length — reported as
/// [`receipt_base`](Executor::receipt_base) — names exactly what aged
/// out.
///
/// The forcing pair — [`Command::ForcePoint`] /
/// [`Command::UnforcePoint`] — is the persistent sibling of a write:
/// it targets the same writable `In` surface and applies at the same
/// boundary, but instead of staging one value it pins the point. While
/// a force stands, the scan's input phase never reads the driver for
/// that point — the image holds the forced value stamped
/// [`Quality::Uncertain`]`(`[`QualityReason::Substituted`]`)` every
/// scan, so components and monitoring see substitution rather than
/// false `Good` data — and an internal-link route onto the point is
/// likewise overridden. A `WriteValue` to a forced field point still
/// reaches the driver — the force overrides the image, not the field —
/// so the release observes whatever the field then carries; the same
/// write to a forced *internal* point refuses at validation with
/// [`CommandError::PointForced`] — the image the force owns is the
/// point's only store, so the input phase would re-stamp the forced
/// value over the staged write within the same scan and an `Applied`
/// settlement would journal an effect that never lands. Release is
/// the same boundary in reverse: the applying scan's input phase reads
/// the driver again for a field point, while a held internal point's
/// image — left with the force's last `Substituted` stamp — is
/// re-stamped `Good`, resuming the held-value rule as the same
/// observable state a `WriteValue` of that value produces. Forces are
/// run state — listed in
/// [`snapshot`](Executor::snapshot)'s `forces` section and carried in
/// [`checkpoint`](Executor::checkpoint) so a standby preserves them.
///
/// Nothing reads a wall clock: identical driver behavior over identical
/// scans produces identical samples on every host.
pub struct Executor<'d> {
    driver: &'d (dyn IoDriver + Sync),
    /// The driver's cyclic-exchange surface when it implements the
    /// [`CyclicIoDriver`] contract — detected at wiring so
    /// [`scan`](Executor::scan) calls `exchange` once per scan at the
    /// read boundary. `None` keeps the per-point driver semantics every
    /// existing driver kind has.
    cyclic: Option<&'d (dyn CyclicIoDriver + Sync)>,
    map: PointMap,
    components: Vec<Entry>,
    image: RefCell<HashMap<PointId, Sample>>,
    /// The active force set: each point pinned to the value the input
    /// phase substitutes for its driver read, in ascending id order so
    /// snapshots and checkpoints serialize deterministically.
    forces: BTreeMap<PointId, Value>,
    /// Indices into `receipts` of the queued commands awaiting their scan
    /// boundary; the command itself rides inside its receipt. Bounded by
    /// `command_capacity`: admission past the bound is refused at
    /// submission with [`CommandError::QueueFull`], so submissions can
    /// never pile up unbounded between scans.
    pending_commands: VecDeque<usize>,
    /// The declared pending-command bound — construction configuration
    /// set through [`with_command_queue_capacity`](Executor::with_command_queue_capacity),
    /// not run state: checkpoints do not carry it.
    command_capacity: usize,
    /// The queue's admission counters, reported through the snapshot's
    /// `command_queue` section and carried in checkpoints beside the
    /// receipt log they measure — the pair's one command-ingress audit.
    /// `attempts` doubles as the log's absolute high-water mark: every
    /// submission produced exactly one receipt, so the entry at
    /// position `i` carries submission index `receipt_base + i`.
    command_admission: CommandAdmissionCounts,
    /// The retained tail of the run's receipt log, in submission order.
    /// Bounded by `receipt_capacity`: once the log outgrows the bound
    /// the leading settled entries evict oldest-first — a receipt still
    /// `Accepted` is pending command state and never evicts, so the
    /// log holds at most `capacity + pending` entries. The count of
    /// evicted entries is [`receipt_base`](Executor::receipt_base) —
    /// `attempts` minus the retained length.
    receipts: Vec<CommandReceipt>,
    /// The declared receipt-log bound — construction configuration set
    /// through [`with_receipt_log_capacity`](Executor::with_receipt_log_capacity),
    /// not run state: checkpoints do not carry it.
    receipt_capacity: usize,
    /// The events components emitted during the most recent scan —
    /// drained per component after its `step`, in scan and emission
    /// order — awaiting the scan's recording. Cleared when the next
    /// scan starts and by a checkpoint apply, which converges the run
    /// to a line that does not carry the abandoned scan's emissions, so
    /// the buffer always holds exactly one scan's events.
    emitted: Vec<EmittedEvent>,
    /// The `KindDeclared`-command availability verdicts the last scan's
    /// probe produced — one probe call per declared `KindDeclared`
    /// command per component, evaluated at the end of the scan's step
    /// phase where component state has settled, and carried verbatim
    /// into the snapshot's `command_verdicts` section. Like `emitted`
    /// the buffer is a scan product: empty before the first scan and
    /// cleared by a checkpoint apply, which converges the run to a line
    /// whose verdicts the adopted state's next scan re-derives.
    command_verdicts: Vec<ComponentCommands>,
    /// The executor-collected half of the snapshot's `io_health` section:
    /// the boundary counters and the fed overrun count. Its `driver`
    /// field stays `None` here — [`snapshot`](Executor::snapshot) fills
    /// it from the driver's `diagnostics` hook at reporting time.
    io_health: IoHealth,
    /// Per-point freshness evidence for field `In` points carrying a
    /// `stale_after_ticks` budget — the driver sample last observed and
    /// the scan tick that observation last changed, kept in the run's
    /// own tick domain so a driver stamping in a foreign domain (a
    /// remote plant's, say) cannot strand the verdict. Run-local
    /// observation state: checkpoints neither carry nor reset it.
    freshness: HashMap<PointId, Freshness>,
    /// The first point whose output write the shared field fenced —
    /// answered [`IoError::Fenced`] — during the most recent scan:
    /// `None` before the first scan and on scans with no fenced write.
    /// A scan product like `emitted`: cleared when the next scan starts
    /// and by a checkpoint apply, which converges the run to a line that
    /// did not see the abandoned scan's fencing. [`Peer`](crate::Peer)
    /// reads it to journal the claim loss a degraded scan still carries.
    fenced_write: Option<PointId>,
    /// The rollback records for the commands the most recent command
    /// boundary applied — one [`BoundaryUndo`] per staged effect, in
    /// application order. A scan product like `emitted`: cleared when
    /// the next scan starts and by a checkpoint apply. Only
    /// [`supersede_commands`](Self::supersede_commands) consumes it —
    /// the fencing path replays it in reverse so the abandoned run's
    /// state, and every checkpoint it keeps serving, agree with the
    /// `Rejected`/`Superseded` receipts: the line never made the
    /// change.
    boundary_undo: Vec<BoundaryUndo>,
    /// The fingerprint of the model this run was assembled from, when
    /// the assembling layer supplied one: stamped into every checkpoint
    /// and the value a restored checkpoint's fingerprint must equal.
    model_fingerprint: Option<ModelFingerprint>,
    /// The tick-domain generation this run's checkpoint stream belongs
    /// to — the identity the assembling shell mints through
    /// [`with_generation`](Self::with_generation) and every checkpoint
    /// stamps. `apply`, `restore`, and `reinitialize` adopt the
    /// checkpoint's: the run joins the line the captured state came
    /// from, so the whole tracked line shares one generation across
    /// switchovers while a cold-started or replaced source begins a new
    /// one. `None` while the run was never given one — the unminted
    /// test/legacy shape — or when the last adoption carried none.
    generation: Option<u64>,
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
            let parameter_defaults = component.report_parameters();
            entries.push(Entry {
                component,
                declared,
                parameter_defaults,
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
            cyclic: driver.cyclic(),
            map,
            components: entries,
            image,
            pending_commands: VecDeque::new(),
            command_capacity: DEFAULT_COMMAND_QUEUE_CAPACITY,
            command_admission: CommandAdmissionCounts::default(),
            receipts: Vec::new(),
            receipt_capacity: DEFAULT_RECEIPT_LOG_CAPACITY,
            emitted: Vec::new(),
            command_verdicts: Vec::new(),
            forces: BTreeMap::new(),
            io_health: IoHealth::default(),
            freshness: HashMap::new(),
            fenced_write: None,
            boundary_undo: Vec::new(),
            model_fingerprint: None,
            generation: None,
            tick: Tick::ZERO,
        })
    }

    /// Records the fingerprint of the model this run was assembled from.
    ///
    /// The assembling layer supplies it — the executor is model-agnostic
    /// and never derives one. Once set, every
    /// [`checkpoint`](Executor::checkpoint) carries it and every
    /// [`restore`](Executor::restore)/[`apply`](Executor::apply) requires
    /// the incoming checkpoint's fingerprint to equal it, so a standby
    /// rejects a checkpoint captured under a different model by name
    /// rather than discovering the skew through structural drift.
    pub fn with_model_fingerprint(mut self, fingerprint: ModelFingerprint) -> Self {
        self.model_fingerprint = Some(fingerprint);
        self
    }

    /// Records this run's checkpoint-stream generation — the tick-domain
    /// identity every [`checkpoint`](Executor::checkpoint) stamps.
    ///
    /// The running process's shell mints it
    /// ([`mint_generation`](crate::mint_generation)) at startup: each
    /// process boot begins a new tick domain, so the generation is
    /// supplied per run, never derived. The mint stays out of
    /// `Executor::new` itself so a run assembled without one — tests,
    /// lone runs — still produces fully deterministic checkpoints. A
    /// later [`apply`](Executor::apply), [`restore`](Executor::restore),
    /// or [`reinitialize`](Executor::reinitialize) adopts the
    /// checkpoint's generation instead: the run joins the line the
    /// captured state came from, so the whole tracked line shares one
    /// generation across a switchover while a cold-started or replaced
    /// source begins a new one.
    pub fn with_generation(mut self, generation: u64) -> Self {
        self.generation = Some(generation);
        self
    }

    /// The generation this run's checkpoint stream belongs to — the
    /// value [`with_generation`](Self::with_generation) recorded or the
    /// last adoption carried; `None` while unidentified.
    pub(crate) fn generation(&self) -> Option<u64> {
        self.generation
    }

    /// Declares the pending-command queue's capacity bound — how many
    /// accepted commands may queue awaiting their scan boundary before
    /// [`submit_command`](Self::submit_command) refuses admission with a
    /// [`CommandError::QueueFull`] rejection receipt. The default is
    /// [`DEFAULT_COMMAND_QUEUE_CAPACITY`]; `0` refuses every admission.
    ///
    /// The bound is construction configuration, not run state: a
    /// checkpoint does not carry it, so an [`Executor::restore`]d or
    /// reconfigured run re-declares it through this method. A pending
    /// set adopted from a checkpoint is exempt — carried entries are
    /// preserved verbatim and still settle at their boundaries — so a
    /// queue restored at or over the bound simply admits nothing new
    /// until a scan drains it below the bound.
    pub fn with_command_queue_capacity(mut self, capacity: usize) -> Self {
        self.command_capacity = capacity;
        self
    }

    /// The declared pending-command bound — what
    /// [`with_command_queue_capacity`](Self::with_command_queue_capacity)
    /// set, or [`DEFAULT_COMMAND_QUEUE_CAPACITY`].
    pub fn command_queue_capacity(&self) -> usize {
        self.command_capacity
    }

    /// Declares the receipt log's retention bound — how many of the
    /// most recent receipts the log keeps once the settled prefix
    /// evicts oldest-first. The default is
    /// [`DEFAULT_RECEIPT_LOG_CAPACITY`]; `0` retains only receipts
    /// still `Accepted` — pending command state never evicts, so the
    /// log holds at most `capacity + pending` entries either way.
    ///
    /// The bound is construction configuration, not run state: a
    /// checkpoint does not carry it, so an [`Executor::restore`]d or
    /// reconfigured run re-declares it through this method. A log
    /// adopted from a checkpoint is trimmed to this run's own bound on
    /// adoption, so a checkpoint captured under a looser bound cannot
    /// grow this log past it.
    pub fn with_receipt_log_capacity(mut self, capacity: usize) -> Self {
        self.receipt_capacity = capacity;
        self
    }

    /// The declared receipt-log bound — what
    /// [`with_receipt_log_capacity`](Self::with_receipt_log_capacity)
    /// set, or [`DEFAULT_RECEIPT_LOG_CAPACITY`].
    pub fn receipt_log_capacity(&self) -> usize {
        self.receipt_capacity
    }

    /// The absolute submission index of `receipts()[0]` — equivalently,
    /// the count of settled receipts the log has already evicted.
    ///
    /// Every submission produced exactly one receipt, so the log is a
    /// contiguous window over the run's submission sequence:
    /// `receipts()[i]` is submission `receipt_base() + i`, and the
    /// admission counter `attempts` is the window's high-water mark
    /// (`receipt_base() + receipts().len()`). A consumer comparing its
    /// last-known index against `receipt_base` reads evictions as a
    /// numbering gap — the same convention the history, journal, and
    /// publication streams use.
    pub fn receipt_base(&self) -> u64 {
        self.command_admission
            .attempts
            .saturating_sub(self.receipts.len() as u64)
    }

    /// The fingerprint this run was assembled with — the value
    /// [`with_model_fingerprint`](Self::with_model_fingerprint) recorded.
    pub fn model_fingerprint(&self) -> Option<ModelFingerprint> {
        self.model_fingerprint
    }

    /// The executor's current virtual tick: [`Tick::ZERO`] before the first
    /// scan, thereafter the tick the last scan ran at.
    pub fn tick(&self) -> Tick {
        self.tick
    }

    /// The first point the last scan's output phase saw the shared field
    /// fence — the write answered [`IoError::Fenced`], meaning the claim
    /// this run held was preempted — or `None` when no write was fenced.
    /// The scan degrades and completes either way; this marker is how a
    /// field-owning [`Peer`](crate::Peer) tells the claim loss apart from
    /// ordinary field trouble so it can journal it once per held claim.
    pub fn fenced_write(&self) -> Option<PointId> {
        self.fenced_write
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

    /// The resolved point map the executor was wired against — the
    /// assembled declaration of each point's direction, kind, and flags
    /// (`writable`, `stale_after_ticks`, `journaled`). The monitor's
    /// recorder reads it for the declared-`journaled` set its
    /// value-transition diff covers.
    pub fn point_map(&self) -> &PointMap {
        &self.map
    }

    /// The driver the executor's scans read and write through — the
    /// write gate when the run was assembled behind one. Exposed for the
    /// redundancy machinery's field comparisons, which read `Out` points
    /// back through this same surface.
    pub fn driver(&self) -> &'d (dyn IoDriver + Sync) {
        self.driver
    }

    /// The staged field-`Out` image: the samples the last scan's write
    /// phase issued toward the driver, keyed by point — what a quiesced
    /// standby would have written had its gate been open. Internal `Out`
    /// points are image-carried and never reach a driver, so like
    /// [`write_outputs`](Self::write_outputs) the map excludes them;
    /// a field `Out` point no component has written yet reports no
    /// sample, matching the write phase's own leave-untouched rule.
    ///
    /// The redundancy machinery stashes one per scan while the peer does
    /// not own the field and compares it, at checkpoint-transfer time,
    /// against the field's actual values — the standby-divergence check.
    pub fn staged_field_outputs(&self) -> BTreeMap<PointId, Sample> {
        let image = self.image.borrow();
        self.map
            .iter()
            .filter(|(_, spec)| spec.direction == Direction::Out && spec.internal.is_none())
            .filter_map(|(point, _)| image.get(&point).map(|&sample| (point, sample)))
            .collect()
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
    /// diagnoses — with each port annotated by its bound point: the
    /// serving layer joins a port to the point its same-named declared
    /// [`IoRequirement`] resolved to, so a monitoring UI renders the
    /// port's live value without re-resolving the model's wiring.
    /// The `forces` section lists the active force set — each forced
    /// point with the value the image substitutes — so a monitoring
    /// consumer can badge forced points. The `parameters` section
    /// reports each component's current parameter values — its
    /// [`report_parameters`](Component::report_parameters) result
    /// filtered to the names its descriptor declares, so only declared
    /// names appear even when a kind's internal state holds more — in
    /// the same scan order as `components` and `descriptors`. The
    /// `command_queue` section reports the pending-command queue's
    /// admission metrics — attempts, full-queue rejections, the declared
    /// capacity, and the queue's depth and high-water mark. The
    /// `command_verdicts` section carries each component's probed
    /// `KindDeclared`-command availability — the verdicts the last
    /// completed scan's step-end evaluation produced, in the same scan
    /// order — empty before the first scan and just after a checkpoint
    /// apply, whose adopted line re-derives them on its next scan.
    pub fn snapshot(&self) -> TelemetrySnapshot {
        let image = self.image.borrow();
        let descriptors: Vec<_> = self
            .components
            .iter()
            .map(|entry| {
                let mut descriptor = entry.component.describe();
                // Serving-layer annotation: a port's `point` is the
                // bound point of its same-named declared I/O
                // requirement; a port naming no declared requirement
                // is unwired and reports `None`.
                let bound: HashMap<String, PointId> = entry
                    .component
                    .io_requirements()
                    .into_iter()
                    .map(|requirement| (requirement.name, requirement.point))
                    .collect();
                for port in &mut descriptor.ports {
                    port.point = bound.get(&port.name).copied();
                }
                descriptor
            })
            .collect();
        // The parameter section reports each component's
        // `report_parameters` filtered to the names its descriptor
        // declares — only declared names appear even when a kind's
        // internal state holds more.
        let parameters = self
            .components
            .iter()
            .zip(&descriptors)
            .map(|(entry, descriptor)| ComponentParameters {
                name: entry.component.name().to_string(),
                values: entry
                    .component
                    .report_parameters()
                    .iter()
                    .filter(|(name, _)| {
                        descriptor
                            .parameters
                            .iter()
                            .any(|parameter| parameter.name == *name)
                    })
                    .map(|(name, value)| (name.to_string(), value))
                    .collect(),
            })
            .collect();
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
            descriptors,
            parameters,
            io_health: IoHealth {
                driver: self.driver.diagnostics(),
                ..self.io_health.clone()
            },
            forces: self
                .forces
                .iter()
                .map(|(&point, &value)| ForcedPoint { point, value })
                .collect(),
            command_verdicts: self.command_verdicts.clone(),
            command_queue: CommandQueueDiagnostics {
                attempts: self.command_admission.attempts,
                full_rejections: self.command_admission.full_rejections,
                capacity: self.command_capacity,
                depth: self.pending_commands.len(),
                high_water: self.command_admission.high_water,
            },
            // The executor reports no publication section: only a
            // monitor's post-scan publication stamps the store's
            // overload counters — this view is the executor's own.
            publication: None,
        }
    }

    /// Queues `command` for application at the start of the next scan and
    /// returns its receipt.
    ///
    /// Submission validates the command — a `WriteValue`,
    /// `ForcePoint`, or `UnforcePoint` against the point map (the point
    /// must be served, must be a writable `In` point, and for the
    /// value-carrying pair the declared kind must match both the
    /// map's kind and the supplied value's variant; a `WriteValue`
    /// additionally refuses an internal point a force currently pins,
    /// [`CommandError::PointForced`], since the force owns the point's
    /// only store until release), a `SetParameter`
    /// against the addressed component's
    /// descriptor (the component must be registered, the parameter
    /// declared, the value's kind matching, and a declared
    /// [`ParameterRange`](dcs_core::ParameterRange) satisfied) — so an
    /// invalid command is [`CommandOutcome::Rejected`] immediately and
    /// never queued. An [`CommandOutcome::Accepted`] receipt reports the
    /// tick the command is scheduled to apply at; at the head of the next
    /// [`scan`](Executor::scan), before the input-read phase, that same
    /// log entry's outcome is updated to [`CommandOutcome::Applied`], or
    /// `Rejected` when the driver refuses the write, the component
    /// refuses the tuned value, or a force queued ahead of a write in
    /// the same boundary pinned its internal point first — exactly one
    /// receipt per command, kept
    /// in the [`receipts`](Executor::receipts) log in submission order.
    /// The log is bounded — settled receipts evict oldest-first past
    /// [`receipt_log_capacity`](Executor::receipt_log_capacity), a
    /// pending receipt never evicts — so an old receipt's place in the
    /// audit expires while the command's settlement itself is already
    /// durable in the journal's `CommandSettled` stream.
    ///
    /// Admission is bounded: a validated command submitted while
    /// `command_capacity` commands are already queued is refused with a
    /// [`CommandError::QueueFull`] rejection receipt — naming the bound —
    /// and queues nothing, so ingress can never grow the pending set
    /// unbounded between scans. Validation still precedes admission: a
    /// statically invalid command takes its named validation rejection
    /// even when the queue is full.
    ///
    /// The receipt is unattributed; [`submit_command_as`](Self::submit_command_as)
    /// is the attributed variant the audit path submits through.
    pub fn submit_command(&mut self, command: Command) -> CommandReceipt {
        self.submit_command_as(command, None)
    }

    /// As [`submit_command`](Self::submit_command), stamping the receipt
    /// with the submitter's declared actor identity — the
    /// audit-attribution field of the command-path audit-identity
    /// decision. The actor is submission metadata: it rides the receipt
    /// untouched by validation, so an attributed command validates,
    /// queues, and settles exactly as an unattributed one, and the
    /// journaled `CommandSettled` echoing this receipt carries the
    /// attribution. `None` submits unattributed — identical to
    /// [`submit_command`](Self::submit_command).
    pub fn submit_command_as(&mut self, command: Command, actor: Option<String>) -> CommandReceipt {
        self.command_admission.attempts += 1;
        let outcome = match self.check_command(&command) {
            Err(reason) => CommandOutcome::Rejected { reason },
            // Validation precedes admission: a statically invalid
            // command takes its named rejection even when the queue is
            // full, and a full queue refuses a valid command without
            // queueing it — never fire-and-forget.
            Ok(_) if self.pending_commands.len() >= self.command_capacity => {
                self.command_admission.full_rejections += 1;
                CommandOutcome::Rejected {
                    reason: CommandError::QueueFull {
                        point: command.point(),
                        capacity: self.command_capacity,
                    },
                }
            }
            Ok(_) => CommandOutcome::Accepted {
                apply_tick: Tick(self.tick.0 + 1),
            },
        };
        let accepted = matches!(outcome, CommandOutcome::Accepted { .. });
        let receipt = CommandReceipt {
            command,
            outcome,
            actor,
        };
        self.receipts.push(receipt.clone());
        if accepted {
            self.pending_commands.push_back(self.receipts.len() - 1);
            self.command_admission.high_water = self
                .command_admission
                .high_water
                .max(self.pending_commands.len());
        }
        self.trim_receipts();
        receipt
    }

    /// Evicts the log's settled prefix past `receipt_capacity` — the
    /// bounded-retention rule [`DEFAULT_RECEIPT_LOG_CAPACITY`]
    /// documents — and returns the count evicted.
    ///
    /// Pending receipts form the log's suffix: a receipt still
    /// `Accepted` is the pending queue's payload, never evictable, so
    /// the leading run of settled entries is all the trim may take.
    /// Pending-queue entries index into `receipts`, so a drain shifts
    /// each by the evicted count; the evicted prefix holds only settled
    /// entries, so no pending index underflows.
    fn trim_receipts(&mut self) -> usize {
        let excess = self.receipts.len().saturating_sub(self.receipt_capacity);
        if excess == 0 {
            return 0;
        }
        let settled = self
            .receipts
            .iter()
            .position(|receipt| matches!(receipt.outcome, CommandOutcome::Accepted { .. }))
            .unwrap_or(self.receipts.len());
        let evicted = excess.min(settled);
        if evicted == 0 {
            return 0;
        }
        self.receipts.drain(..evicted);
        for index in &mut self.pending_commands {
            *index -= evicted;
        }
        evicted
    }

    /// The receipt log's retained tail: the most recent receipts in
    /// submission order, bounded by
    /// [`receipt_log_capacity`](Executor::receipt_log_capacity) — see
    /// [`DEFAULT_RECEIPT_LOG_CAPACITY`] for the eviction rule and its
    /// visibility through `command_queue.attempts` and
    /// [`receipt_base`](Executor::receipt_base).
    ///
    /// A queued command's entry reads [`CommandOutcome::Accepted`] until
    /// the scan boundary updates it to `Applied` or `Rejected`, so the
    /// log is the audit trail a monitoring consumer reads alongside the
    /// snapshot.
    pub fn receipts(&self) -> &[CommandReceipt] {
        &self.receipts
    }

    /// The events the last completed scan's components emitted, in the
    /// order they were drained — component scan order, then each
    /// component's own emission order — each stamped with the producing
    /// component's registered name. The recording layer attributes them
    /// to the producing scan's tick and routes them by their declared
    /// [`EventRetention`](dcs_core::EventRetention). Empty before the
    /// first scan and after a checkpoint apply converged the run — the
    /// adopted line's emissions are its own, not the abandoned scan's.
    pub fn emitted_events(&self) -> &[EmittedEvent] {
        &self.emitted
    }

    /// Records one scan cycle that overran its wall-clock period — the
    /// documented write path by which the pacing shell (the controller's
    /// scan loop) feeds the snapshot's `io_health.scan_overruns`.
    ///
    /// The executor itself never detects overruns: its clock is virtual
    /// ticks, so pacing lives in the wall-clock shell, which compares a
    /// cycle's `Instant` elapsed against its configured period and calls
    /// this once per overran cycle. The call only increments a counter —
    /// it is a report into telemetry, not a scan input, so component
    /// determinism is untouched. The feed goes through the same wrappers
    /// the shell already scans through (`Peer::record_scan_overrun`,
    /// `Monitor::record_scan_overrun`).
    pub fn record_scan_overrun(&mut self) {
        self.io_health.scan_overruns += 1;
    }

    /// Runs `scans` scans and returns the tick the last one ran at.
    ///
    /// `run(0)` is a no-op returning the current tick.
    pub fn run(&mut self, scans: u64) -> Tick {
        for _ in 0..scans {
            self.scan();
        }
        self.tick
    }

    /// Executes one scan — apply commands, read inputs, step components,
    /// write outputs — and returns the tick it ran at. See the type docs
    /// for the phase order. Field-side faults never abort the scan:
    /// they degrade into `io_health` counters and held `Bad` samples,
    /// so the returned tick always reports a completed scan.
    pub fn scan(&mut self) -> Tick {
        self.tick = Tick(self.tick.0 + 1);
        let tick = self.tick;

        self.emitted.clear();
        self.fenced_write = None;
        self.boundary_undo.clear();
        self.apply_commands(tick);
        self.exchange_image(tick);
        self.read_inputs(tick);
        self.step_components(tick);
        self.probe_command_verdicts();
        self.write_outputs();
        tick
    }

    /// Executes one quiesced scan — the same read → step → write cycle
    /// as [`scan`](Self::scan) but without the scan-head command
    /// application.
    ///
    /// A peer that does not own the field
    /// ([`Peer::scan`](crate::Peer::scan)) runs this: an adopted
    /// still-`Accepted` receipt is carried for a possible promotion, not
    /// settled here — a quiesced scan must not mint an `Applied` the line
    /// never ordered. The pending queue and the receipt log pass through
    /// untouched, so the carried entries settle once at the promoted
    /// run's first field-owning scan.
    pub fn scan_quiesced(&mut self) -> Tick {
        self.tick = Tick(self.tick.0 + 1);
        let tick = self.tick;

        self.emitted.clear();
        self.fenced_write = None;
        self.boundary_undo.clear();
        self.exchange_image(tick);
        self.read_inputs(tick);
        self.step_components(tick);
        self.probe_command_verdicts();
        self.write_outputs();
        tick
    }

    /// Captures the run's transferable state as a [`Checkpoint`].
    ///
    /// The checkpoint bundles the current tick, every component's
    /// [`capture_state`](Component::capture_state) keyed by name (empty
    /// for stateless components), the driver's captured state when it
    /// implements the contract, the image-carried point samples: the
    /// `Out` samples — the last written output values — plus the internal
    /// `In` samples, so held operator values and link carriers transfer,
    /// and the command receipt log's retained tail — the bounded audit
    /// [`DEFAULT_RECEIPT_LOG_CAPACITY`] describes — so `GET /receipts`
    /// answers identically on a peer that adopted the checkpoint. It is
    /// serde-serializable, so an active
    /// controller can ship it to a standby over the same JSON channel the
    /// monitoring contract uses. See the [`Checkpoint`] docs for how this
    /// maps to real redundancy.
    pub fn checkpoint(&self) -> Checkpoint {
        let image = self.image.borrow();
        Checkpoint {
            format_version: CHECKPOINT_FORMAT_VERSION,
            model_fingerprint: self.model_fingerprint,
            generation: self.generation,
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
            forces: self.forces.clone(),
            receipts: self.receipts.clone(),
            command_admission: self.command_admission,
            // The executor has no role view — the serving `Peer` stamps
            // `source_owns_field` over its own capture; `line_proof`
            // exists only on `?prove=` responses, never on a capture.
            source_owns_field: None,
            line_proof: None,
        }
    }

    /// Rebuilds an executor equivalent to the one `checkpoint` captured.
    ///
    /// `driver`, `map`, and `components` are the same inputs
    /// [`Executor::new`] takes — on a standby they are built from the same
    /// plant model — and `model_fingerprint` is the fingerprint that
    /// model carries, supplied by the assembling layer exactly as
    /// [`with_model_fingerprint`](Self::with_model_fingerprint) records
    /// it. Restore wires the components, negotiates the checkpoint's
    /// format version and fingerprint against `model_fingerprint`, checks
    /// that the checkpoint's component set equals the registered set by
    /// name, applies the driver state when the checkpoint carries one,
    /// restores each component's state, then resumes the tick and the
    /// output image. The result is an executor whose next
    /// [`scan`](Executor::scan) produces outputs identical to the
    /// captured run's, itself fingerprinted so the checkpoints it later
    /// emits carry the same identity.
    ///
    /// Any incompatibility fails with a [`RestoreError`] naming the
    /// element at fault: an unsupported format version or a fingerprint
    /// mismatch before any state is examined, then a [`WiringError`], a
    /// component-name mismatch, a component's
    /// [`StateError`](dcs_core::StateError), or the driver's. No
    /// partially restored executor is returned; the supplied driver is
    /// asked to validate before applying its state section.
    pub fn restore(
        driver: &'d (dyn IoDriver + Sync),
        map: PointMap,
        components: Vec<Box<dyn Component>>,
        checkpoint: &Checkpoint,
        model_fingerprint: Option<ModelFingerprint>,
    ) -> Result<Self, RestoreError> {
        let mut executor = Self::new(driver, map, components)?;
        executor.model_fingerprint = model_fingerprint;
        executor.check_checkpoint(checkpoint)?;

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
        // The restored run joins the checkpointed line's generation —
        // a `--state-file` resume continues the same tick domain, so the
        // checkpoints it serves carry the line's identity, not a fresh
        // one.
        executor.generation = checkpoint.generation;
        executor.image.borrow_mut().extend(
            checkpoint
                .outputs
                .iter()
                .chain(checkpoint.internal.iter())
                .map(|(&point, &sample)| (point, sample)),
        );
        executor.forces = checkpoint.forces.clone();
        executor.adopt_receipts(checkpoint);
        Ok(executor)
    }

    /// Applies `checkpoint` to this executor in place — the running
    /// standby's half of the redundancy contract.
    ///
    /// Where [`restore`](Executor::restore) builds a fresh equivalent
    /// executor, `apply` realigns one that is already assembled and may
    /// be mid-run: the same compatibility checks hold — the checkpoint's
    /// format version must be supported and its model fingerprint must
    /// equal this run's, its
    /// component set must equal the registered set, its outputs must be
    /// points the map serves as `Out` with the declared kinds, and its
    /// internal section must name image-carried `In` points — then the
    /// driver and each component restore their captured state, the tick
    /// resumes from `checkpoint.tick`, the output image becomes
    /// exactly the checkpoint's while its internal `In` samples overlay
    /// the image's held values, and the receipt log becomes the
    /// checkpoint's — the pair's one command audit, entries still
    /// `Accepted` re-queued for this run's next boundary — extended by
    /// this run's own still-unreached tail when the adopted window's
    /// submission high-water never saw it (see
    /// [`adopt_receipts`](Self::adopt_receipts)). The next
    /// [`scan`](Executor::scan) then
    /// continues the run the checkpoint captured.
    ///
    /// Like `restore`, a rejected apply changes nothing the run
    /// observes: the executor captures its own state first and rolls
    /// back to it when any element refuses its section, so a mismatched
    /// checkpoint leaves a standby on its last-good alignment rather
    /// than half-applied.
    pub fn apply(&mut self, checkpoint: &Checkpoint) -> Result<(), RestoreError> {
        self.check_checkpoint(checkpoint)?;

        // Capture the current state for rollback: a map an element
        // captured from itself always restores cleanly.
        let driver_backup = self.driver.capture_state();
        let component_backups: Vec<StateMap> = self
            .components
            .iter()
            .map(|entry| entry.component.capture_state())
            .collect();

        // Driver before components, as in `restore`: the contract
        // validates the whole map before applying, so a rejection here
        // leaves the driver untouched.
        if let Some(state) = &checkpoint.driver {
            self.driver
                .restore_state(state)
                .map_err(RestoreError::Driver)?;
        }
        for (index, entry) in self.components.iter_mut().enumerate() {
            let state = checkpoint
                .components
                .get(entry.component.name())
                .expect("checked above");
            if let Err(error) = entry.component.restore_state(state) {
                for (restored, backup) in
                    self.components[..index].iter_mut().zip(&component_backups)
                {
                    let _ = restored.component.restore_state(backup);
                }
                if checkpoint.driver.is_some()
                    && let Some(backup) = &driver_backup
                {
                    let _ = self.driver.restore_state(backup);
                }
                return Err(RestoreError::Component(error));
            }
        }

        self.tick = checkpoint.tick;
        // The run joins the checkpointed line's generation: from this
        // adoption on, the checkpoints this executor serves name the
        // line's tick-domain identity, so a peer tracking it can tell
        // the line's continuation from a new generation's stream.
        self.generation = checkpoint.generation;
        let mut image = self.image.borrow_mut();
        // The output image becomes exactly the checkpoint's: drop stale
        // `Out` samples so a value from the standby's own earlier scans
        // cannot linger where the captured run never wrote. Internal `In`
        // samples overlay rather than replace: a checkpoint carrying an
        // `internal` section converges the standby's held values to the
        // active's — a commanded setpoint included — while one without it
        // (written before internal points existed) leaves the standby's
        // last-known values standing.
        image.retain(|point, _| {
            self.map
                .get(*point)
                .is_none_or(|spec| spec.direction != Direction::Out)
        });
        image.extend(
            checkpoint
                .outputs
                .iter()
                .chain(checkpoint.internal.iter())
                .map(|(&point, &sample)| (point, sample)),
        );
        drop(image);
        // The checkpoint's force set is authoritative: the standby
        // forces exactly what the active forced — no more, no less.
        self.forces.clone_from(&checkpoint.forces);
        // The last scan's drained emissions and probed command verdicts
        // belong to the abandoned line: the adopted run's records start
        // empty — the next scan re-derives the verdicts from the adopted
        // component state.
        self.emitted.clear();
        self.command_verdicts.clear();
        self.fenced_write = None;
        self.boundary_undo.clear();
        self.adopt_receipts(checkpoint);
        Ok(())
    }

    /// Adopts a checkpoint's receipt log — the pair's one command
    /// audit, converging `GET /receipts` on every peer — and re-queues
    /// the entries still `Accepted` at capture. The adopted window is
    /// re-trimmed to this run's own `receipt_capacity` — settled
    /// prefix first, pending entries never — so a checkpoint captured
    /// under a looser bound cannot grow this log past its own. A
    /// command taken over between its submission boundary and its
    /// applying scan is run state like the image's: the restoring run
    /// applies it at its own next boundary, so a switchover mid-flight
    /// never drops it. The admission counters converge with the audit
    /// they measure, so the pair's `command_queue` telemetry section
    /// answers identically.
    ///
    /// One stretch of this run's own log survives the convergence:
    /// the contiguous suffix the adopted window's high-water mark —
    /// `attempts`, the submission index one past the last the source
    /// recorded — does not reach. A receipt at or beyond that mark is
    /// a submission the checkpoint's source had not observed at
    /// capture, so its absence from the adopted log is the checkpoint's
    /// staleness, not the line's verdict; dropping it would settle a
    /// provisional absence as if it were one. The suffix — a demoted
    /// run's suspended commands and the settled receipts below them —
    /// is restored verbatim behind the adopted window and `attempts`
    /// rises to cover it, so the checkpoint this run keeps serving
    /// still offers the entries to a successor's
    /// [`carry_pending_commands`](Self::carry_pending_commands) pull.
    /// Restored `Accepted` entries stay suspended — they do not
    /// re-queue, a quiesced scan must not mint an `Applied` the line
    /// never ordered — and resolve when a covering adoption
    /// adjudicates their indices: carried then, settling with the
    /// line, or passed by and abandoned. A suffix whose base index
    /// sits past the adopted high-water — evictions the source never
    /// saw opening a gap the log cannot span — cannot be restored and
    /// drops with the rest of the abandoned window.
    fn adopt_receipts(&mut self, checkpoint: &Checkpoint) {
        // The adopted window's end in the submission sequence — the
        // high-water a prior receipt's index measures against. Indices
        // at or beyond it extend the adopted log contiguously only
        // while the run's window reaches back to meet it: a prior base
        // above the mark leaves a gap no restoration can span.
        let adopted_end = checkpoint.receipt_base() + checkpoint.receipts.len() as u64;
        let uncovered: Vec<CommandReceipt> = if adopted_end >= self.receipt_base() {
            let skip = (adopted_end - self.receipt_base()).min(self.receipts.len() as u64) as usize;
            self.receipts[skip..].to_vec()
        } else {
            Vec::new()
        };
        self.receipts.clone_from(&checkpoint.receipts);
        // The adopted log is re-trimmed to this run's own bound: a
        // checkpoint captured under a looser capacity cannot grow this
        // log past it, and the pending queue rebuilds over the trimmed
        // log's own indices. The stale queue clears first — its indices
        // addressed the abandoned log.
        self.pending_commands.clear();
        self.command_admission = checkpoint.command_admission;
        self.trim_receipts();
        self.pending_commands = self
            .receipts
            .iter()
            .enumerate()
            .filter(|(_, receipt)| matches!(receipt.outcome, CommandOutcome::Accepted { .. }))
            .map(|(index, _)| index)
            .collect();
        // Carried entries are adopted verbatim past the bound: they are
        // run state, not new admission, so a restored queue at or over
        // capacity still settles them at their boundaries while refusing
        // new submissions until a scan drains it. The adoption is real
        // depth and joins the high-water record.
        self.command_admission.high_water = self
            .command_admission
            .high_water
            .max(self.pending_commands.len());
        if !uncovered.is_empty() {
            // The restored suffix lands after the queue rebuild on
            // purpose: its `Accepted` entries are suspended state the
            // tracked line has not adjudicated, not carried commands
            // owed a boundary, so they must not re-queue. `attempts`
            // rises to the window's true high-water — the submissions
            // this log still holds — keeping `receipt_base` honest and
            // the served checkpoint carrying them for a successor's
            // carry.
            self.command_admission.attempts = adopted_end + uncovered.len() as u64;
            self.receipts.extend(uncovered);
            self.trim_receipts();
        }
    }

    /// Adopts the admissions a checkpoint's receipt log carries past
    /// this run's own — the promote boundary's stale-tick path
    /// ([`Peer::final_sync`](crate::Peer::final_sync)): a checkpoint
    /// older than the run's tick cannot [`apply`](Self::apply) without
    /// rewinding scans the peer already ran — replaying their commands
    /// and re-emitting their events — but the commands the tracked
    /// active admitted since this run's last alignment still must not
    /// be lost.
    ///
    /// The receipt log is a contiguous window over the line's
    /// submission sequence — entry `i` carries index
    /// `receipt_base + i`, and the window's end is the admission
    /// high-water — so the receipts the checkpoint holds beyond this
    /// window's end are exactly the submissions this run never saw.
    /// Each lands verbatim — the pair's one command audit — and the
    /// entries still [`CommandOutcome::Accepted`] queue for this run's
    /// next boundary, settling there like any adopted pending command,
    /// past the admission bound just as under [`apply`](Self::apply).
    /// The overlapping stretch is this run's log already — settlements
    /// included — and stays untouched: a command this line settled is
    /// never re-queued, so nothing applies twice. Entries the source
    /// already evicted below its own window stay evicted here too — a
    /// numbering gap the `attempts` counters report, not a recoverable
    /// stretch. The admission counters measuring the adopted log
    /// converge with it.
    pub fn carry_pending_commands(&mut self, checkpoint: &Checkpoint) {
        let self_end = self.receipt_base() + self.receipts.len() as u64;
        let checkpoint_base = checkpoint.receipt_base();
        let checkpoint_end = checkpoint_base + checkpoint.receipts.len() as u64;
        if checkpoint_end <= self_end {
            return;
        }
        let tail = self_end.max(checkpoint_base);
        let skipped = (tail - checkpoint_base) as usize;
        let base_len = self.receipts.len();
        self.receipts
            .extend(checkpoint.receipts[skipped..].iter().cloned());
        self.command_admission = checkpoint.command_admission;
        // Bound the union before the adopted `Accepted` entries queue:
        // the trim may reach into the tail's own settled prefix, so the
        // surviving adopted entries start at `base_len - evicted`.
        let evicted = self.trim_receipts();
        for (index, receipt) in self
            .receipts
            .iter()
            .enumerate()
            .skip(base_len.saturating_sub(evicted))
        {
            if matches!(receipt.outcome, CommandOutcome::Accepted { .. }) {
                self.pending_commands.push_back(index);
            }
        }
        self.command_admission.high_water = self
            .command_admission
            .high_water
            .max(self.pending_commands.len());
    }

    /// Consumes a checkpoint captured under a *different* model — the
    /// model-boundary carryover a revision-armed peer runs instead of
    /// [`apply`](Self::apply), whose fingerprint gate would refuse it.
    ///
    /// The rule is documented in [`crate::revision`]: writable internal
    /// `In` points and `Out` image samples matched by declared identity
    /// carry their last values, forces carry all-or-nothing, the receipt
    /// log carries verbatim — the run's audit survives the boundary,
    /// entries still `Accepted` re-queued to settle under the revision —
    /// component and driver state reinitialize, and the tick resumes at
    /// the checkpoint's. The report itemizes the tuning the reinitialize
    /// rule reverted: per reinitialized component, every
    /// descriptor-declared parameter whose checkpointed value differs
    /// from the revision's declared default. The rule classifies the
    /// whole checkpoint before any
    /// state moves, so a revision that breaks it — a kind-retyped
    /// carried point, an unservable force, an unreadable format — fails
    /// with a named [`CarryoverError`] and changes nothing the run
    /// observes.
    ///
    /// Unlike `apply`, success does not claim equivalence to the
    /// captured run: the first post-promotion scan computes fresh
    /// outputs from reinitialized components over the carried image.
    /// The returned [`CarryoverReport`] is the audit record — what
    /// carried, what initialized, what was named dropped — and rides the
    /// peer's reported [`StandbySync::Reinitialized`](dcs_core::StandbySync)
    /// state.
    pub fn reinitialize(
        &mut self,
        checkpoint: &Checkpoint,
    ) -> Result<CarryoverReport, CarryoverError> {
        if !SUPPORTED_FORMAT_VERSIONS.contains(&checkpoint.format_version) {
            return Err(CarryoverError::UnsupportedVersion {
                found: checkpoint.format_version,
                supported: SUPPORTED_FORMAT_VERSIONS,
            });
        }

        // Classify first, apply second: every named failure is detected
        // before any state moves, so a refused crossing leaves the run
        // untouched.
        let mut carried = Vec::new();
        let mut carried_outputs = Vec::new();
        let mut carried_samples = Vec::new();
        let mut dropped = Vec::new();
        for (&point, &sample) in &checkpoint.internal {
            match self.map.get(point) {
                Some(spec)
                    if spec.direction == Direction::In
                        && spec.internal.is_some()
                        && spec.writable =>
                {
                    if spec.kind != sample.value.kind() {
                        return Err(CarryoverError::InternalKindMismatch {
                            point,
                            expected: spec.kind,
                            found: sample.value,
                        });
                    }
                    carried.push(CarriedPoint {
                        point,
                        value: sample.value,
                    });
                    carried_samples.push((point, sample));
                }
                _ => dropped.push(DroppedElement::InternalPoint { point }),
            }
        }
        for (&point, &sample) in &checkpoint.outputs {
            match self.map.get(point) {
                Some(spec) if spec.direction == Direction::Out => {
                    if spec.kind != sample.value.kind() {
                        return Err(CarryoverError::OutputKindMismatch {
                            point,
                            expected: spec.kind,
                            found: sample.value,
                        });
                    }
                    carried_outputs.push(CarriedPoint {
                        point,
                        value: sample.value,
                    });
                    carried_samples.push((point, sample));
                }
                _ => dropped.push(DroppedElement::OutputPoint { point }),
            }
        }
        for (&point, &value) in &checkpoint.forces {
            match self.map.get(point) {
                Some(spec) if spec.direction == Direction::In && spec.writable => {
                    if spec.kind != value.kind() {
                        return Err(CarryoverError::ForceKindMismatch {
                            point,
                            expected: spec.kind,
                            found: value,
                        });
                    }
                }
                _ => return Err(CarryoverError::ForceNotServed { point }),
            }
        }
        for name in checkpoint.components.keys() {
            if !self
                .components
                .iter()
                .any(|entry| entry.component.name() == name)
            {
                dropped.push(DroppedElement::Component { name: name.clone() });
            }
        }
        if checkpoint.driver.is_some() {
            dropped.push(DroppedElement::DriverState);
        }

        // The reverted-tuning itemization: every reinitialized
        // component's descriptor-declared parameters whose checkpointed
        // field differs from the revision's declared default — the
        // value the component's fresh construction reported, captured
        // at wiring — are named so the report says which tuning was
        // lost, not just which components restarted. The baseline is
        // the captured defaults, not the live `report_parameters`: an
        // `Accepted` receipt the crossing adopted can re-settle the
        // same tune on this run, and a re-report that diffed the live
        // value would then name nothing. Component state never crosses,
        // so a checkpointed field the descriptor does not declare is
        // the component's own dropped vocabulary, and a checkpointed
        // field whose kind the revision retyped itemizes like any
        // differing value — nothing is reinterpreted, so the
        // named-refusal convention does not apply.
        let mut reverted_tuning = Vec::new();
        for entry in &self.components {
            let component = &entry.component;
            let declared_parameters = component.describe().parameters;
            if declared_parameters.is_empty() {
                continue;
            }
            let Some(checkpointed) = checkpoint.components.get(component.name()) else {
                continue;
            };
            for parameter in declared_parameters {
                let (Some(checkpointed_value), Some(declared_value)) = (
                    checkpointed.get(&parameter.name),
                    entry.parameter_defaults.get(&parameter.name),
                ) else {
                    continue;
                };
                if !parameter_value_stands(checkpointed_value, declared_value) {
                    reverted_tuning.push(RevertedParameter {
                        component: component.name().to_string(),
                        parameter: parameter.name,
                        checkpointed: checkpointed_value,
                        declared: declared_value,
                    });
                }
            }
        }

        // Apply: carried samples land verbatim — quality and tick as the
        // checkpoint captured them — over the image's seeded initials;
        // the force set becomes exactly the checkpoint's; the run's
        // numbering resumes at the checkpointed tick. Components and the
        // driver are untouched: their state is the revision's fresh
        // construction by rule.
        {
            let mut image = self.image.borrow_mut();
            image.extend(carried_samples);
        }
        self.forces.clone_from(&checkpoint.forces);
        self.tick = checkpoint.tick;
        // The crossing keeps the tracked line's generation: the revised
        // run continues the checkpoint stream's tick domain, so the
        // checkpoints it serves still name the line they came from.
        self.generation = checkpoint.generation;
        self.emitted.clear();
        self.command_verdicts.clear();
        self.fenced_write = None;
        self.adopt_receipts(checkpoint);

        let initialized = self
            .map
            .iter()
            .filter(|(_, spec)| {
                spec.direction == Direction::In && spec.internal.is_some() && spec.writable
            })
            .map(|(point, _)| point)
            .filter(|point| !carried.iter().any(|carried| carried.point == *point))
            .collect();
        Ok(CarryoverReport {
            from: checkpoint.model_fingerprint,
            to: self.model_fingerprint,
            resumed_at: checkpoint.tick,
            carried,
            carried_outputs,
            carried_forces: checkpoint
                .forces
                .iter()
                .map(|(&point, &value)| ForcedPoint { point, value })
                .collect(),
            dropped,
            reinitialized: self
                .components
                .iter()
                .map(|entry| entry.component.name().to_string())
                .collect(),
            reverted_tuning,
            initialized,
        })
    }

    /// The compatibility half of checkpoint restore and apply: the
    /// checkpoint's format version must be one this build accepts and its
    /// model fingerprint must equal this run's — the explicit negotiation
    /// that runs before any state is examined — then the checkpoint's
    /// component set must equal the registered set exactly,
    /// every captured output must name a point the map serves as `Out`
    /// with the declared value kind, and every captured internal sample
    /// must name an image-carried `In` point with the declared kind.
    fn check_checkpoint(&self, checkpoint: &Checkpoint) -> Result<(), RestoreError> {
        // Negotiation first: a checkpoint this build cannot read, or one
        // captured under a different model, is refused before any state
        // applies.
        if !SUPPORTED_FORMAT_VERSIONS.contains(&checkpoint.format_version) {
            return Err(RestoreError::UnsupportedVersion {
                found: checkpoint.format_version,
                supported: SUPPORTED_FORMAT_VERSIONS,
            });
        }
        if checkpoint.model_fingerprint != self.model_fingerprint {
            return Err(RestoreError::FingerprintMismatch {
                found: checkpoint.model_fingerprint,
                expected: self.model_fingerprint,
            });
        }
        // Component names are the run's component ids: the checkpoint's
        // set must equal the registered set exactly.
        for component in checkpoint.components.keys() {
            if !self
                .components
                .iter()
                .any(|entry| entry.component.name() == component)
            {
                return Err(RestoreError::UnknownComponent {
                    component: component.clone(),
                });
            }
        }
        for entry in &self.components {
            if !checkpoint.components.contains_key(entry.component.name()) {
                return Err(RestoreError::MissingComponent {
                    component: entry.component.name().to_string(),
                });
            }
        }
        for (&point, &sample) in &checkpoint.outputs {
            match self.map.get(point) {
                Some(spec) if spec.direction == Direction::Out => {
                    if spec.kind != sample.value.kind() {
                        return Err(RestoreError::IncompatibleOutput {
                            point,
                            expected: spec.kind,
                            found: sample.value,
                        });
                    }
                }
                _ => return Err(RestoreError::UnknownOutput { point }),
            }
        }
        for (&point, &sample) in &checkpoint.internal {
            match self.map.get(point) {
                Some(spec) if spec.direction == Direction::In && spec.internal.is_some() => {
                    if spec.kind != sample.value.kind() {
                        return Err(RestoreError::IncompatibleInternal {
                            point,
                            expected: spec.kind,
                            found: sample.value,
                        });
                    }
                }
                _ => return Err(RestoreError::UnknownInternal { point }),
            }
        }
        for (&point, &value) in &checkpoint.forces {
            match self.map.get(point) {
                Some(spec) if spec.direction == Direction::In && spec.writable => {
                    if spec.kind != value.kind() {
                        return Err(RestoreError::IncompatibleForce {
                            point,
                            expected: spec.kind,
                            found: value,
                        });
                    }
                }
                _ => return Err(RestoreError::UnknownForce { point }),
            }
        }
        Ok(())
    }

    /// Validates `command` and resolves what it will apply. The checks
    /// are static — the map fixes which points exist, which of them are
    /// writable `In` points, and their declared kinds, and a component's
    /// descriptor fixes which parameters exist, their kinds, and their
    /// declared ranges — plus one run-state check: whether a force
    /// currently pins an internal point a `WriteValue` targets, which a
    /// force queued ahead of it in the same boundary can newly answer,
    /// so a submission's `Accepted` can still settle `Rejected` at
    /// application when the boundary's own ordering forces the point
    /// first.
    ///
    /// A `WriteValue` must name a served point ([`CommandError::UnknownPoint`])
    /// the map marks writable and whose direction is `In`
    /// ([`CommandError::NotWritable`] otherwise — every `Out` point
    /// refuses writes), then the declared kind must match the map's and
    /// the supplied value's variant ([`CommandError::TypeMismatch`]).
    /// One state check follows the static ones: a `WriteValue` to an
    /// *internal* point a force currently pins refuses with
    /// [`CommandError::PointForced`] — the image is the point's only
    /// store and the force owns it until release, so nothing the write
    /// staged could ever land — while a forced *field* point's write
    /// still resolves, the driver holding it for the release to
    /// observe. `ForcePoint`/`UnforcePoint` share the writable surface:
    /// a force pins a point only the operator could write, so the same
    /// `UnknownPoint`/`NotWritable`/`TypeMismatch` rejections bound it —
    /// and a force never touches the driver, so no boundary refusal
    /// exists for the pair.
    ///
    /// A `SetParameter` resolves its component by
    /// [`name`](Component::name) — the identity the descriptor and the
    /// snapshot's diagnostics report — then validates against the
    /// component's declared parameters: a component declaring none is
    /// [`CommandError::UnsupportedParameter`], an undeclared name is
    /// [`CommandError::UnknownParameter`], a mismatched value kind is
    /// [`CommandError::ParameterTypeMismatch`], and a value outside a
    /// declared [`ParameterRange`](dcs_core::ParameterRange) is
    /// [`CommandError::OutOfRange`].
    ///
    /// An `Invoke` resolves its component the same way, then validates
    /// against the component's declared commands: an undeclared command
    /// is [`CommandError::UnknownCommand`] and a supplied argument whose
    /// kind differs from its declaration is
    /// [`CommandError::ArgumentTypeMismatch`]. A well-formed invocation
    /// resolves to [`Resolved::Invoke`], which the applying scan
    /// dispatches to the component's
    /// [`invoke_command`](Component::invoke_command) hook — the declared
    /// availability predicate and the kind's own invariants decide
    /// there, and a refusal settles the receipt
    /// [`CommandError::CommandRefused`] carrying the kind's reason.
    fn check_command(&self, command: &Command) -> Result<Resolved, CommandError> {
        match command {
            Command::WriteValue { point, kind, value }
            | Command::ForcePoint { point, kind, value } => {
                let spec = self.check_command_point(*point)?;
                if *kind != spec.kind {
                    return Err(CommandError::TypeMismatch {
                        point: *point,
                        expected: spec.kind,
                        found: *value,
                    });
                }
                if value.kind() != *kind {
                    return Err(CommandError::TypeMismatch {
                        point: *point,
                        expected: *kind,
                        found: *value,
                    });
                }
                // A write to a forced *internal* point cannot land: the
                // image is the point's only store and the force owns it
                // — the input phase re-stamps the forced value over the
                // staged write within the same scan — so the receipted
                // path refuses rather than settle `Applied` for an
                // effect nothing can observe. A forced *field* point's
                // write still resolves: the driver keeps it for the
                // release to read back.
                if matches!(command, Command::WriteValue { .. })
                    && spec.internal.is_some()
                    && self.forces.contains_key(point)
                {
                    return Err(CommandError::PointForced { point: *point });
                }
                Ok(match command {
                    Command::ForcePoint { .. } => Resolved::Force {
                        point: *point,
                        value: *value,
                    },
                    _ => Resolved::Write {
                        point: *point,
                        value: *value,
                    },
                })
            }
            Command::UnforcePoint { point } => {
                self.check_command_point(*point)?;
                Ok(Resolved::Unforce { point: *point })
            }
            Command::SetParameter {
                component,
                name,
                value,
            } => {
                let index = self
                    .components
                    .iter()
                    .position(|entry| entry.component.name() == component)
                    .ok_or_else(|| CommandError::UnknownComponent {
                        component: component.clone(),
                    })?;
                let parameters = self.components[index].component.describe().parameters;
                let declared = match parameters.iter().find(|p| p.name == *name) {
                    Some(declared) => declared,
                    None if parameters.is_empty() => {
                        return Err(CommandError::UnsupportedParameter {
                            component: component.clone(),
                            parameter: name.clone(),
                        });
                    }
                    None => {
                        return Err(CommandError::UnknownParameter {
                            component: component.clone(),
                            parameter: name.clone(),
                        });
                    }
                };
                if value.kind() != declared.kind {
                    return Err(CommandError::ParameterTypeMismatch {
                        component: component.clone(),
                        parameter: name.clone(),
                        expected: declared.kind,
                        found: *value,
                    });
                }
                if let Some(range) = declared.range
                    && !range.contains(*value)
                {
                    return Err(CommandError::OutOfRange {
                        component: component.clone(),
                        parameter: name.clone(),
                        value: *value,
                        range,
                    });
                }
                Ok(Resolved::Parameter {
                    component: index,
                    name: name.clone(),
                    value: *value,
                })
            }
            Command::Invoke {
                component,
                command: name,
                arguments,
            } => {
                let index = self
                    .components
                    .iter()
                    .position(|entry| entry.component.name() == component)
                    .ok_or_else(|| CommandError::UnknownComponent {
                        component: component.clone(),
                    })?;
                let declared = self.components[index].component.describe().commands;
                let Some(spec) = declared.iter().find(|entry| entry.name == *name) else {
                    return Err(CommandError::UnknownCommand {
                        component: component.clone(),
                        command: name.clone(),
                    });
                };
                for (argument, value) in arguments {
                    if let Some(declared) =
                        spec.request.iter().find(|entry| entry.name == *argument)
                        && declared.kind != value.kind()
                    {
                        return Err(CommandError::ArgumentTypeMismatch {
                            component: component.clone(),
                            command: name.clone(),
                            argument: argument.clone(),
                            expected: declared.kind,
                            found: value.kind(),
                        });
                    }
                }
                Ok(Resolved::Invoke {
                    component: index,
                    command: name.clone(),
                    arguments: arguments.clone(),
                })
            }
        }
    }

    /// The surface check every point command shares: the point must be
    /// served ([`CommandError::UnknownPoint`]) and must be a writable
    /// `In` point — the command surface is the map's writable `In`
    /// points, so an unmarked point, and every `Out` point, refuses
    /// before its payload is examined with [`CommandError::NotWritable`].
    fn check_command_point(&self, point: PointId) -> Result<PointSpec, CommandError> {
        let spec = self
            .map
            .get(point)
            .ok_or(CommandError::UnknownPoint { point })?;
        if spec.direction != Direction::In || !spec.writable {
            return Err(CommandError::NotWritable { point });
        }
        Ok(spec)
    }

    /// Applies every queued command in submission order, updating each
    /// one's receipt to its final outcome. Runs at the head of the scan —
    /// before the input read — so a write to a writable `In` point is
    /// observed by this scan's input phase. Only writable `In` writes
    /// reach here: submission already refused every other point.
    ///
    /// A command to an internal point writes the image directly — the
    /// driver does not serve it — so a held `In` value changes here and
    /// holds until the next command. A write to an internal point a
    /// force pins never reaches here: validation refuses it
    /// [`CommandError::PointForced`], at submission or at this boundary
    /// when a force queued ahead of it just landed, because the force's
    /// input-phase substitution would erase the staged value before any
    /// publication could observe it.
    ///
    /// A `SetParameter` lands on the component's
    /// [`apply_parameter`](Component::apply_parameter) hook at this same
    /// boundary; a hook refusal settles the receipt `Rejected` and
    /// changes nothing.
    ///
    /// An `Invoke` lands on the component's
    /// [`invoke_command`](Component::invoke_command) hook here too: the
    /// submission-time declaration checks have already run, so the
    /// hook's `Err` is the declared availability predicate or a kind
    /// invariant speaking — the receipt settles `Rejected` with a
    /// [`CommandError::CommandRefused`] carrying the kind's reason
    /// verbatim.
    ///
    /// A `ForcePoint` records the force — this scan's input phase already
    /// substitutes the value at `Substituted` quality — and an
    /// `UnforcePoint` lifts it, so the same scan reads the driver again
    /// for a field point, while a held internal point's image — left with
    /// the force's last `Substituted` stamp — is re-stamped `Good` here:
    /// the held-value rule resuming as the observable state a same-value
    /// `WriteValue` produces. The pair never touches the driver, so both
    /// settle `Applied` here.
    fn apply_commands(&mut self, tick: Tick) {
        while let Some(index) = self.pending_commands.pop_front() {
            let command = self.receipts[index].command.clone();
            // The schedule the admitting line set: a queued entry is
            // still `Accepted`, and a carried one replays on the
            // promoted run scans after its `apply_tick`. An internal
            // point's image sample keeps that stamp — the held value's
            // line time — so consumers aging a standing request measure
            // the operator-visible window rather than the handover lag.
            let scheduled = match self.receipts[index].outcome {
                CommandOutcome::Accepted { apply_tick } => apply_tick.min(tick),
                _ => tick,
            };
            self.receipts[index].outcome = match self.check_command(&command) {
                Err(reason) => CommandOutcome::Rejected { reason },
                Ok(Resolved::Write { point, value }) => {
                    let internal = self
                        .map
                        .get(point)
                        .is_some_and(|spec| spec.internal.is_some());
                    if internal {
                        self.boundary_undo.push(BoundaryUndo::Image {
                            point,
                            prior: self.image.borrow().get(&point).copied(),
                        });
                        self.image
                            .borrow_mut()
                            .insert(point, Sample::good(value, scheduled));
                        CommandOutcome::Applied { tick }
                    } else {
                        // The image still holds the last-observed field
                        // sample at the boundary head — this scan's
                        // input phase has not run yet — which is the
                        // prior a supersede's write-back restores. A
                        // forced point's image holds the substituted
                        // value instead — the field's own value went
                        // unobserved — so no write-back can honestly
                        // claim a prior.
                        let prior = if self.forces.contains_key(&point) {
                            None
                        } else {
                            self.image.borrow().get(&point).copied()
                        };
                        match self.driver.write(point, value) {
                            Err(error) => CommandOutcome::Rejected {
                                reason: CommandError::DriverRejected { point, error },
                            },
                            Ok(()) => {
                                self.boundary_undo
                                    .push(BoundaryUndo::FieldWrite { point, prior });
                                self.image
                                    .borrow_mut()
                                    .insert(point, Sample::good(value, tick));
                                CommandOutcome::Applied { tick }
                            }
                        }
                    }
                }
                Ok(Resolved::Parameter {
                    component,
                    name,
                    value,
                }) => {
                    self.note_component_undo(component);
                    match self.components[component]
                        .component
                        .apply_parameter(&name, value)
                    {
                        Ok(()) => CommandOutcome::Applied { tick },
                        Err(reason) => CommandOutcome::Rejected { reason },
                    }
                }
                Ok(Resolved::Force { point, value }) => {
                    self.boundary_undo.push(BoundaryUndo::Force {
                        point,
                        prior: self.forces.get(&point).copied(),
                    });
                    // The force's image effect lands at this scan's
                    // input phase — the rollback owes the pre-scan
                    // sample to a held internal point, whose image no
                    // later channel rewrites.
                    if self
                        .map
                        .get(point)
                        .is_some_and(|spec| spec.internal.is_some())
                    {
                        self.boundary_undo.push(BoundaryUndo::Image {
                            point,
                            prior: self.image.borrow().get(&point).copied(),
                        });
                    }
                    self.forces.insert(point, value);
                    CommandOutcome::Applied { tick }
                }
                Ok(Resolved::Unforce { point }) => {
                    let removed = self.forces.remove(&point);
                    self.boundary_undo.push(BoundaryUndo::Force {
                        point,
                        prior: removed,
                    });
                    if removed.is_some()
                        && self
                            .map
                            .get(point)
                            .is_some_and(|spec| spec.internal.is_some())
                    {
                        // No channel rewrites a held internal point, so
                        // the lifted force's last `Substituted` stamp
                        // would stand forever — re-stamp the held value
                        // `Good`: the observable state a same-value
                        // `WriteValue` produces. A field point needs
                        // nothing here; this scan's input phase reads
                        // the driver again.
                        let held = self.image.borrow().get(&point).copied();
                        self.boundary_undo
                            .push(BoundaryUndo::Image { point, prior: held });
                        if let Some(sample) = held {
                            self.image
                                .borrow_mut()
                                .insert(point, Sample::good(sample.value, tick));
                        }
                    }
                    CommandOutcome::Applied { tick }
                }
                Ok(Resolved::Invoke {
                    component,
                    command,
                    arguments,
                }) => {
                    self.note_component_undo(component);
                    match self.components[component]
                        .component
                        .invoke_command(&command, &arguments)
                    {
                        Ok(()) => CommandOutcome::Applied { tick },
                        Err(reason) => CommandOutcome::Rejected {
                            reason: CommandError::CommandRefused {
                                component: self.components[component].component.name().to_string(),
                                command,
                                reason,
                            },
                        },
                    }
                }
            };
        }
        // Settlements landed: the entries the boundary just resolved
        // rejoin the evictable prefix, so the trim runs here too —
        // the log returns to its bound at the boundary rather than
        // waiting for a later submission to shrink it.
        self.trim_receipts();
    }

    /// Captures the component at `index` for the boundary's rollback
    /// record — once per boundary per component, before the first
    /// command touches it — so [`supersede_commands`](Self::supersede_commands)
    /// can return every mutation the abandoned boundary staged.
    fn note_component_undo(&mut self, index: usize) {
        if self.boundary_undo.iter().any(
            |undo| matches!(undo, BoundaryUndo::Component { index: captured, .. } if *captured == index),
        ) {
            return;
        }
        let state = self.components[index].component.capture_state();
        self.boundary_undo
            .push(BoundaryUndo::Component { index, state });
    }

    /// Reconciles the commands a superseded run must not report applied.
    ///
    /// [`Peer`](crate::Peer) runs this on the scan that discovered the
    /// lost field claim: the boundary settled the run's queued commands
    /// onto an image the field will never see — the claim already
    /// belonged to another attachment — so `Applied` would overstate
    /// what an auditing operator reads as "took effect". Every receipt
    /// still `Accepted` in the pending queue, and every receipt the
    /// boundary at `tick` settled `Applied`, is rewritten `Rejected`
    /// carrying [`CommandError::Superseded`]. Settlements of earlier
    /// boundaries — applied while the run still owned the field — stand,
    /// as do the boundary's own refusals (a field-point write the fence
    /// already answered `DriverRejected`).
    ///
    /// The receipt rewrite is only half the reconciliation: the state
    /// those commands mutated is run state — internal `In` image
    /// samples, the force set, component state — and this demoted run
    /// keeps serving it in quiesced checkpoints a tracking successor
    /// adopts. A `Rejected`/`Superseded` receipt means the surviving
    /// line never made the change, so the boundary's staged effects
    /// replay back out in reverse application order — the rollback
    /// records [`apply_commands`](Self::apply_commands) captured — and
    /// the checkpoints this run serves from here carry the
    /// pre-boundary state. A field-side write the driver accepted gets
    /// a best-effort compensating write-back: the field's own claim
    /// arbitration may refuse it — the claim is already lost — which is
    /// the plant keeping its own record of what landed.
    pub fn supersede_commands(&mut self, tick: Tick) {
        while let Some(index) = self.pending_commands.pop_front() {
            self.receipts[index].outcome = CommandOutcome::Rejected {
                reason: CommandError::Superseded {
                    point: self.receipts[index].command.point(),
                },
            };
        }
        for receipt in &mut self.receipts {
            if receipt.outcome == (CommandOutcome::Applied { tick }) {
                receipt.outcome = CommandOutcome::Rejected {
                    reason: CommandError::Superseded {
                        point: receipt.command.point(),
                    },
                };
            }
        }
        // Unstage what the superseded boundary staged — reverse
        // application order, so a chain of writes to one point unwinds
        // to the sample the boundary found. The records belong to this
        // tick's boundary alone: `scan` clears the list each cycle and
        // `apply_commands` is the only producer.
        for undo in self.boundary_undo.drain(..).rev() {
            match undo {
                BoundaryUndo::Image { point, prior } => {
                    let mut image = self.image.borrow_mut();
                    match prior {
                        Some(sample) => {
                            image.insert(point, sample);
                        }
                        None => {
                            image.remove(&point);
                        }
                    }
                }
                BoundaryUndo::Force { point, prior } => match prior {
                    Some(value) => {
                        self.forces.insert(point, value);
                    }
                    None => {
                        self.forces.remove(&point);
                    }
                },
                BoundaryUndo::FieldWrite { point, prior } => {
                    if let Some(sample) = prior {
                        let _ = self.driver.write(point, sample.value);
                    }
                }
                BoundaryUndo::Component { index, state } => {
                    // A state map the component captured from itself
                    // restores cleanly — the same contract `apply`'s
                    // rollback relies on.
                    let _ = self.components[index].component.restore_state(&state);
                }
            }
        }
        self.trim_receipts();
    }

    /// Suspends the run's queued commands without settling them — the
    /// demotion counterpart of [`supersede_commands`](Self::supersede_commands).
    /// [`Peer::demote`](crate::Peer::demote) runs it as the gate closes:
    /// the receipts stay `Accepted` in the log, so the checkpoints this
    /// run keeps serving still carry them for a successor's
    /// promotion-boundary pull ([`Peer::final_sync`](crate::Peer::final_sync),
    /// [`carry_pending_commands`](Self::carry_pending_commands)) — but
    /// the demoted run's own scans no longer apply them, because a
    /// quiesced scan's `Applied` would journal an application the gate
    /// kept off the field, erased by the next adoption. Each suspended
    /// entry resolves on the tracked line's checkpoint applies: covered
    /// by the adopted log it re-queues and settles with the run; passed
    /// by the adopted window's high-water without being carried, the
    /// peer settles it `Rejected` carrying
    /// [`CommandError::Superseded`]. An adoption whose high-water has
    /// not reached the entry's index settles nothing — the source had
    /// not observed the submission at capture, so
    /// [`adopt_receipts`](Self::adopt_receipts) restores it suspended
    /// for a covering checkpoint to adjudicate — a pending command
    /// neither vanishes unaudited, reports `applied` on an abandoned
    /// image, nor settles `superseded` on a verdict the line never
    /// made.
    pub fn suspend_pending_commands(&mut self) {
        self.pending_commands.clear();
    }

    /// The cyclic exchange at the read boundary: when the driver
    /// implemented [`CyclicIoDriver`] at wiring, one `exchange` call —
    /// after command application, so a command-staged write publishes in
    /// the same exchange, and before the per-point input reads that then
    /// serve the image it latched. A failed exchange is counted once —
    /// under `failed_exchanges`, the consecutive-failure streak, and
    /// `last_error`, attributed `In` because it opens the input phase —
    /// and never aborts the scan: the driver's held input image still
    /// answers the reads that follow, aging under each point's declared
    /// `stale_after_ticks` budget until the driver's declared
    /// `exchange_miss_threshold` escalates its reads to ordinary
    /// per-point failures. A driver without a cyclic surface skips the
    /// phase entirely.
    fn exchange_image(&mut self, tick: Tick) {
        let Some(cyclic) = self.cyclic else {
            return;
        };
        match cyclic.exchange(tick) {
            Ok(()) => self.io_health.consecutive_failures = 0,
            Err(error) => {
                self.io_health.failed_exchanges += 1;
                self.io_health.consecutive_failures += 1;
                self.io_health.last_error = Some(IoFault {
                    tick,
                    point: error.point(),
                    direction: Direction::In,
                    error,
                });
            }
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
    ///
    /// A field `In` point carrying a `stale_after_ticks` budget gets the
    /// freshness check before the re-stamp: when the sample the driver
    /// returns has not changed in more than `budget` scan ticks, the
    /// landed sample's quality merges
    /// [`Quality::Uncertain`]`(`[`QualityReason::Stale`]`)` — the
    /// worst-of merge, so a driver-reported `Bad` or worse-named
    /// `Uncertain` is never improved to `Stale`, and the first changed
    /// sample inside the budget again returns the driver's own quality.
    /// The lag is measured in the run's own tick domain — the scans
    /// since the driver report last changed — not against the stamp the
    /// sample carries: a remote driver stamps in the plant's tick
    /// domain, whose offset from the scan tick a stopped-then-resumed
    /// field leaves permanently lagging, so a cross-domain comparison
    /// would latch stale on fresh data. The image stamp stays the scan
    /// tick either way.
    ///
    /// A forced `In` point skips both channels: the driver is not read
    /// — so a field fault on a forced point counts no failed read —
    /// and no link routes onto it. The image instead holds the forced
    /// value stamped `Uncertain(Substituted)` at this scan's tick, every
    /// scan until release.
    fn read_inputs(&mut self, tick: Tick) {
        let mut image = self.image.borrow_mut();
        for (point, spec) in self.map.iter() {
            if spec.direction != Direction::In {
                continue;
            }
            if let Some(&value) = self.forces.get(&point) {
                image.insert(
                    point,
                    Sample::new(value, Quality::Uncertain(QualityReason::Substituted), tick),
                );
                continue;
            }
            if spec.internal.is_some() {
                continue;
            }
            let sample = match self.driver.read(point) {
                Ok(sample) => {
                    self.io_health.consecutive_failures = 0;
                    // Freshness is judged in the run's own tick domain —
                    // the driver-returned sample is change evidence,
                    // never the image's timestamp: a remote driver
                    // stamps in the plant's tick domain, whose offset
                    // from the scan tick a stopped-then-resumed field
                    // leaves permanently lagging, so comparing the two
                    // domains directly would latch stale on fresh data.
                    let quality = match spec.stale_after_ticks {
                        Some(budget) => {
                            let freshness = self.freshness.entry(point).or_insert(Freshness {
                                observed: sample,
                                since: sample.tick.min(tick),
                            });
                            if freshness.observed != sample {
                                *freshness = Freshness {
                                    observed: sample,
                                    since: tick,
                                };
                            }
                            if tick.0.saturating_sub(freshness.since.0) > budget {
                                sample
                                    .quality
                                    .merge(Quality::Uncertain(QualityReason::Stale))
                            } else {
                                sample.quality
                            }
                        }
                        None => sample.quality,
                    };
                    Sample {
                        quality,
                        tick,
                        ..sample
                    }
                }
                Err(error) => {
                    self.io_health.failed_reads += 1;
                    self.io_health.consecutive_failures += 1;
                    self.io_health.last_error = Some(IoFault {
                        tick,
                        point: error.point(),
                        direction: Direction::In,
                        error,
                    });
                    Sample::new(
                        image
                            .get(&point)
                            .map_or(neutral(spec.kind), |last| last.value),
                        failure_quality(error),
                        tick,
                    )
                }
            };
            image.insert(point, sample);
        }
        for (output, input) in self.map.links() {
            if self.forces.contains_key(&input) {
                continue;
            }
            if let Some(sample) = image.get(&output).copied() {
                image.insert(input, Sample { tick, ..sample });
            }
        }
    }

    /// Steps each component in scan order over an image view scoped to its
    /// declared points, then drains its emitted events into the scan's
    /// emitted record — in the component's own emission order, stamped
    /// with its registered name. A failing step is recorded and the scan
    /// continues, and the drain runs on the failure path too: events the
    /// step emitted before reporting are not stranded.
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
            self.emitted
                .extend(entry.component.drain_events().into_iter().map(|mut event| {
                    event.component = entry.component.name().to_string();
                    event
                }));
        }
    }

    /// Re-derives every component's `KindDeclared`-command availability
    /// verdicts for the snapshot's `command_verdicts` section — one
    /// [`command_refusal`](Component::command_refusal) probe call per
    /// declared [`CommandAvailability::KindDeclared`] command, in scan
    /// order.
    ///
    /// Runs at the end of the scan's step phase — inside the scan
    /// boundary where component state has settled for the scan, never
    /// under a consumer read — so the published verdicts are a pure
    /// function of post-step state: a tracking standby's scans derive
    /// identical verdicts from the same adopted state, and a per-peer
    /// driver-boundary failure cannot skew them. The verdicts are
    /// advisory only: a `describe`-declared command whose probe reports
    /// `available` may still be refused at dispatch — by the
    /// argument-dependent checks the probe never sees — and that
    /// refusal settles on the ordinary receipt rather than failing the
    /// scan.
    fn probe_command_verdicts(&mut self) {
        self.command_verdicts = self
            .components
            .iter()
            .map(|entry| {
                let descriptor = entry.component.describe();
                ComponentCommands {
                    name: entry.component.name().to_string(),
                    verdicts: descriptor
                        .commands
                        .iter()
                        .filter(|command| command.availability == CommandAvailability::KindDeclared)
                        .map(|command| {
                            let refusal = entry.component.command_refusal(&command.name);
                            CommandVerdict {
                                name: command.name.clone(),
                                available: refusal.is_none(),
                                refusal,
                            }
                        })
                        .collect(),
                }
            })
            .collect();
    }

    /// Writes every field `Out` point the image holds to the driver.
    /// Points a component never wrote keep no image entry and are left
    /// untouched; internal `Out` points are image-carried for monitoring
    /// and never reach the driver.
    ///
    /// A failed write is the read boundary's mirror: counted under
    /// `failed_writes`, attributed as `last_error`, and the scan
    /// continues — the image still records the intended output, the
    /// field holds its last written value, and a dead link must never
    /// take the controller and its telemetry down with it.
    fn write_outputs(&mut self) {
        let image = self.image.borrow();
        for (point, spec) in self.map.iter() {
            if spec.direction != Direction::Out || spec.internal.is_some() {
                continue;
            }
            let Some(sample) = image.get(&point) else {
                continue;
            };
            match self.driver.write(point, sample.value) {
                Ok(()) => self.io_health.consecutive_failures = 0,
                Err(error) => {
                    if self.fenced_write.is_none()
                        && let IoError::Fenced(point) = error
                    {
                        self.fenced_write = Some(point);
                    }
                    self.io_health.failed_writes += 1;
                    self.io_health.consecutive_failures += 1;
                    self.io_health.last_error = Some(IoFault {
                        tick: self.tick,
                        point: error.point(),
                        direction: Direction::Out,
                        error,
                    });
                }
            }
        }
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
        CommandArgument, CommandAvailability, CommandDecl, ComponentDescriptor, DriverDiagnostics,
        EventDecl, EventField, EventFieldKind, EventRetention, EventValue, ExchangeDiagnostics,
        Input, LinkState, Output, ParameterDescriptor, ParameterRange, PortDescriptor, PortRole,
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
            executor.scan();
            // After one scan the chain has only reached point 20.
            assert_eq!(driver_value(&driver, 20), Value::Float(10.0));
            assert_eq!(driver_value(&driver, 40), Value::Float(0.0));

            driver.advance();
            executor.scan();
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

        ordered(1.0, 2.0).scan();
        assert_eq!(driver_value(&driver, 50), Value::Float(2.0));
        ordered(2.0, 1.0).scan();
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
        assert_eq!(executor.run(0), Tick::ZERO);
        executor.run(3);
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

        executor.scan();
        assert_eq!(
            executor.sample(PointId(10)).unwrap().value,
            Value::Float(7.0)
        );

        driver.faults.lock().unwrap().insert(PointId(10));
        executor.scan();
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

        executor.run(2);
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
        executor.scan();
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
        executor.scan();
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
        executor.scan();
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

        executor.scan();
        driver.faults.lock().unwrap().insert(PointId(10));
        executor.scan();

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
    fn failed_input_reads_count_into_io_health() {
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

        executor.scan();
        // A clean scan reports an all-zero health section — and the stub
        // has no transport, so the driver half is `None`.
        assert_eq!(executor.snapshot().io_health, IoHealth::default());

        driver.faults.lock().unwrap().insert(PointId(10));
        executor.scan();
        executor.scan();

        // The documented read behavior continues — the scan succeeds and
        // the held value degrades to Bad — and each failed read is
        // counted and attributed to its tick and point.
        let health = &executor.snapshot().io_health;
        assert_eq!(health.failed_reads, 2);
        assert_eq!(health.failed_writes, 0);
        assert_eq!(health.consecutive_failures, 2);
        assert_eq!(
            health.last_error,
            Some(IoFault {
                tick: Tick(3),
                point: PointId(10),
                direction: Direction::In,
                error: IoError::Disconnected(PointId(10)),
            })
        );
        assert_eq!(health.driver, None);
        assert_eq!(health.scan_overruns, 0);

        // A successful boundary operation ends the consecutive streak;
        // the counters and last-error attribution persist.
        driver.faults.lock().unwrap().remove(&PointId(10));
        executor.scan();
        let health = &executor.snapshot().io_health;
        assert_eq!(health.failed_reads, 2);
        assert_eq!(health.consecutive_failures, 0);
        assert_eq!(health.last_error.unwrap().tick, Tick(3));
    }

    /// A field `In` point map carrying `stale_after_ticks` — the shape
    /// assembly resolves a budgeted `io_point` declaration into.
    fn stale_map(point: PointId, budget: u64) -> PointMap {
        PointMap::new().with_spec(
            point,
            PointSpec {
                direction: Direction::In,
                kind: ValueKind::Float,
                internal: None,
                writable: false,
                stale_after_ticks: Some(budget),
                journaled: false,
            },
        )
    }

    /// Stamps the stub's field sample for `point` at `tick`, as a device
    /// that refreshed — or stopped refreshing — at that driver tick.
    fn stamp(driver: &StubDriver, point: u64, sample: Sample) {
        driver.points.lock().unwrap().insert(PointId(point), sample);
    }

    #[test]
    fn field_input_within_freshness_budget_stays_good() {
        let driver = StubDriver::new(&[float(10)], &[]);
        let mut executor = Executor::new(
            &driver,
            stale_map(PointId(10), 2),
            vec![Box::new(Declared {
                name: "idle",
                requirements: vec![IoRequirement::input::<f64>("in", PointId(10))],
            })],
        )
        .unwrap();

        // The driver stamped the sample at its tick 0; lags of 1 and 2
        // sit within the budget — the landed quality stays Good and the
        // image stamp is the scan tick, not the driver's.
        executor.scan();
        executor.scan();
        let sample = executor.snapshot().points[0].sample.unwrap();
        assert_eq!(sample, Sample::good(Value::Float(0.0), Tick(2)));

        // A lag exactly at the budget is still fresh.
        stamp(&driver, 10, Sample::good(Value::Float(3.0), Tick(1)));
        executor.scan();
        let sample = executor.snapshot().points[0].sample.unwrap();
        assert_eq!(sample, Sample::good(Value::Float(3.0), Tick(3)));
    }

    #[test]
    fn lagging_field_input_reports_stale_uncertainty() {
        let driver = StubDriver::new(&[float(10)], &[]);
        driver.write(PointId(10), Value::Float(7.0)).unwrap();
        let mut executor = Executor::new(
            &driver,
            stale_map(PointId(10), 2),
            vec![Box::new(Declared {
                name: "idle",
                requirements: vec![IoRequirement::input::<f64>("in", PointId(10))],
            })],
        )
        .unwrap();

        // The driver stopped refreshing after stamping tick 0; the third
        // scan's lag of 3 exceeds the budget of 2.
        executor.run(3);
        let sample = executor.snapshot().points[0].sample.unwrap();
        // The value is preserved and the image stamp is the scan tick —
        // the driver tick is freshness evidence, never the timestamp.
        assert_eq!(sample.value, Value::Float(7.0));
        assert_eq!(sample.quality, Quality::Uncertain(QualityReason::Stale));
        assert_eq!(sample.tick, Tick(3));
    }

    #[test]
    fn fresh_read_recovers_stale_input_to_good() {
        let driver = StubDriver::new(&[float(10)], &[]);
        let mut executor = Executor::new(
            &driver,
            stale_map(PointId(10), 2),
            vec![Box::new(Declared {
                name: "idle",
                requirements: vec![IoRequirement::input::<f64>("in", PointId(10))],
            })],
        )
        .unwrap();

        executor.run(3);
        assert_eq!(
            executor.snapshot().points[0].sample.unwrap().quality,
            Quality::Uncertain(QualityReason::Stale)
        );

        // The device refreshed at its tick 3; the next scan's lag of 1
        // returns the driver's own quality on the first fresh read.
        stamp(&driver, 10, Sample::good(Value::Float(9.0), Tick(3)));
        executor.scan();
        let sample = executor.snapshot().points[0].sample.unwrap();
        assert_eq!(sample, Sample::good(Value::Float(9.0), Tick(4)));
    }

    #[test]
    fn stale_check_keeps_driver_reported_quality() {
        let driver = StubDriver::new(&[float(10)], &[]);
        stamp(
            &driver,
            10,
            Sample::new(
                Value::Float(7.0),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(0),
            ),
        );
        let mut executor = Executor::new(
            &driver,
            stale_map(PointId(10), 2),
            vec![Box::new(Declared {
                name: "idle",
                requirements: vec![IoRequirement::input::<f64>("in", PointId(10))],
            })],
        )
        .unwrap();

        // A lagging Bad sample keeps the driver-reported quality — the
        // worst-of merge never improves it to Uncertain(Stale).
        executor.run(3);
        let sample = executor.snapshot().points[0].sample.unwrap();
        assert_eq!(sample.quality, Quality::Bad(QualityReason::DeviceFault));
        assert_eq!(sample.tick, Tick(3));

        // A lagging Uncertain named worse than Stale keeps its reason.
        stamp(
            &driver,
            10,
            Sample::new(
                Value::Float(8.0),
                Quality::Uncertain(QualityReason::OutOfRange),
                Tick(3),
            ),
        );
        executor.run(2);
        let sample = executor.snapshot().points[0].sample.unwrap();
        assert_eq!(
            sample.quality,
            Quality::Uncertain(QualityReason::OutOfRange)
        );
    }

    #[test]
    fn failed_read_on_budgeted_point_keeps_bad_mapping() {
        let driver = StubDriver::new(&[float(10)], &[]);
        driver.write(PointId(10), Value::Float(7.0)).unwrap();
        let mut executor = Executor::new(
            &driver,
            stale_map(PointId(10), 2),
            vec![Box::new(Declared {
                name: "idle",
                requirements: vec![IoRequirement::input::<f64>("in", PointId(10))],
            })],
        )
        .unwrap();

        // A failed read is not a stale sample: the held value degrades
        // to Bad(CommunicationFault) exactly as on a point without a
        // budget, and the failure counts into I/O health.
        executor.scan();
        driver.faults.lock().unwrap().insert(PointId(10));
        executor.scan();
        let sample = executor.snapshot().points[0].sample.unwrap();
        assert_eq!(sample.value, Value::Float(7.0));
        assert_eq!(
            sample.quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
        assert_eq!(executor.snapshot().io_health.failed_reads, 1);
    }

    #[test]
    fn point_without_freshness_budget_is_never_stale_stamped() {
        let driver = StubDriver::new(&[float(10)], &[]);
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

        // The driver sample's tick lags by any amount — with no declared
        // budget the landed quality stays Good.
        executor.run(10);
        let sample = executor.snapshot().points[0].sample.unwrap();
        assert_eq!(sample, Sample::good(Value::Float(0.0), Tick(10)));
    }

    #[test]
    fn resumed_stamp_advances_clear_stale_despite_a_domain_lag() {
        // The QA finding's shape: the driver stamps in a tick domain
        // that a stopped-then-resumed field leaves permanently lagging
        // the run's — each unstepped window adds its length to the gap.
        // Freshness must follow the report's *change*, not the stamp's
        // offset: a lag measured across the two domains would read
        // `scan_tick - stamp` past the budget forever and latch stale
        // on data arriving every scan.
        let driver = StubDriver::new(&[float(10)], &[]);
        let mut executor = Executor::new(
            &driver,
            stale_map(PointId(10), 2),
            vec![Box::new(Declared {
                name: "idle",
                requirements: vec![IoRequirement::input::<f64>("in", PointId(10))],
            })],
        )
        .unwrap();

        // The field owner steps: the stamp advances every scan and the
        // two domains stay aligned — every read lands fresh.
        for tick in 1..=4 {
            stamp(
                &driver,
                10,
                Sample::good(Value::Float(tick as f64), Tick(tick)),
            );
            executor.scan();
            assert_eq!(
                executor.snapshot().points[0].sample.unwrap().quality,
                Quality::Good
            );
        }

        // The unstepped window — the pause/demote the run ticks through:
        // the driver report stops changing at its tick 4 and the point
        // ages to stale past its budget.
        executor.run(2);
        assert_eq!(
            executor.snapshot().points[0].sample.unwrap().quality,
            Quality::Good
        );
        executor.scan();
        let sample = executor.snapshot().points[0].sample.unwrap();
        assert_eq!(sample.quality, Quality::Uncertain(QualityReason::Stale));
        assert_eq!(sample.tick, Tick(7));

        // The field resumes stepping — the driver domain resumes at its
        // own tick 5, now four behind the run's: a cross-domain lag
        // reads 4 > 2 and would stay stale forever, but the changed
        // report is fresh evidence and the driver's own quality lands.
        stamp(&driver, 10, Sample::good(Value::Float(9.0), Tick(5)));
        executor.scan();
        let sample = executor.snapshot().points[0].sample.unwrap();
        assert_eq!(sample, Sample::good(Value::Float(9.0), Tick(8)));

        // And stays fresh while the resumed domain keeps advancing
        // behind the run's — the permanent gap is not the lag the
        // budget measures.
        stamp(&driver, 10, Sample::good(Value::Float(9.0), Tick(6)));
        executor.run(2);
        assert_eq!(
            executor.snapshot().points[0].sample.unwrap().quality,
            Quality::Good
        );
    }

    #[test]
    fn a_run_started_behind_the_driver_domain_still_marks_stale() {
        // The finding's symmetric edge: a run whose tick sits behind
        // the driver's stamp domain — a resume at a persisted tick the
        // field has already advanced past — used to saturate the lag at
        // zero and never mark stale. Judged in the run's domain, a
        // stalled driver report ages exactly like a lagging one.
        let driver = StubDriver::new(&[float(10)], &[]);
        let mut executor = Executor::new(
            &driver,
            stale_map(PointId(10), 2),
            vec![Box::new(Declared {
                name: "idle",
                requirements: vec![IoRequirement::input::<f64>("in", PointId(10))],
            })],
        )
        .unwrap();

        // The driver domain runs ahead — stamps near its tick 100 while
        // the run is at 1 — and keeps advancing: fresh every scan.
        for offset in 0..3 {
            stamp(
                &driver,
                10,
                Sample::good(Value::Float(7.0), Tick(100 + offset)),
            );
            executor.scan();
            assert_eq!(
                executor.snapshot().points[0].sample.unwrap().quality,
                Quality::Good
            );
        }

        // The driver stalls at its tick 102: the run's own lag accrues
        // and the point presents stale past the budget — the verdict a
        // stamp-domain comparison could never reach from behind.
        executor.run(3);
        assert_eq!(
            executor.snapshot().points[0].sample.unwrap().quality,
            Quality::Uncertain(QualityReason::Stale)
        );

        // A report that resumes advancing clears it again — still ahead
        // of the run's domain, still the driver's own quality.
        stamp(&driver, 10, Sample::good(Value::Float(8.0), Tick(140)));
        executor.scan();
        assert_eq!(
            executor.snapshot().points[0].sample.unwrap(),
            Sample::good(Value::Float(8.0), Tick(7))
        );
    }

    #[test]
    fn a_changed_value_at_a_held_stamp_counts_as_fresh_evidence() {
        // Freshness follows the driver *report*, not the stamp alone: a
        // field write while the driver's tick is frozen changes the
        // report without moving its stamp — new evidence the run counts.
        let driver = StubDriver::new(&[float(10)], &[]);
        let mut executor = Executor::new(
            &driver,
            stale_map(PointId(10), 2),
            vec![Box::new(Declared {
                name: "idle",
                requirements: vec![IoRequirement::input::<f64>("in", PointId(10))],
            })],
        )
        .unwrap();

        // The held tick-0 report ages to stale.
        executor.run(3);
        assert_eq!(
            executor.snapshot().points[0].sample.unwrap().quality,
            Quality::Uncertain(QualityReason::Stale)
        );

        // A changed report at the same stamp is fresh anyway — the
        // driver's domain told the run nothing new could arrive.
        stamp(&driver, 10, Sample::good(Value::Float(9.0), Tick(0)));
        executor.scan();
        assert_eq!(
            executor.snapshot().points[0].sample.unwrap(),
            Sample::good(Value::Float(9.0), Tick(4))
        );
    }

    #[test]
    fn forced_budgeted_input_reports_substituted_not_stale() {
        let driver = StubDriver::new(&[float(10)], &[]);
        let map = PointMap::new().with_spec(
            PointId(10),
            PointSpec {
                direction: Direction::In,
                kind: ValueKind::Float,
                internal: None,
                writable: true,
                stale_after_ticks: Some(2),
                journaled: false,
            },
        );
        let mut executor = Executor::new(
            &driver,
            map,
            vec![Box::new(Declared {
                name: "idle",
                requirements: vec![IoRequirement::input::<f64>("in", PointId(10))],
            })],
        )
        .unwrap();

        executor.submit_command(force_point(10, ValueKind::Float, Value::Float(5.0)));
        executor.run(5);
        // The forced path never reads the driver, so the lagging field
        // sample cannot reach the image — Substituted stands.
        let sample = executor.snapshot().points[0].sample.unwrap();
        assert_eq!(
            sample,
            Sample::new(
                Value::Float(5.0),
                Quality::Uncertain(QualityReason::Substituted),
                Tick(5)
            )
        );
    }

    #[test]
    fn failed_output_writes_count_into_io_health() {
        let driver = StubDriver::new(&[float(20)], &[]);
        let map: PointMap = [(PointId(20), Direction::Out, ValueKind::Float)]
            .into_iter()
            .collect();
        let mut executor = Executor::new(
            &driver,
            map,
            vec![Box::new(Constant {
                name: "const",
                output: PointId(20),
                value: 1.0,
            })],
        )
        .unwrap();

        executor.scan();
        driver.faults.lock().unwrap().insert(PointId(20));

        // The write boundary's degrade rule — the same one reads carry:
        // the failure is counted and attributed while the scan completes.
        executor.scan();
        assert_eq!(executor.tick(), Tick(2));
        let health = &executor.snapshot().io_health;
        assert_eq!(health.failed_writes, 1);
        assert_eq!(health.failed_reads, 0);
        assert_eq!(health.consecutive_failures, 1);
        assert_eq!(
            health.last_error,
            Some(IoFault {
                tick: Tick(2),
                point: PointId(20),
                direction: Direction::Out,
                error: IoError::Disconnected(PointId(20)),
            })
        );

        // The streak ends on the next successful write; the totals stay.
        driver.faults.lock().unwrap().remove(&PointId(20));
        executor.scan();
        let health = &executor.snapshot().io_health;
        assert_eq!(health.failed_writes, 1);
        assert_eq!(health.consecutive_failures, 0);
    }

    /// A stub reporting transport diagnostics, to show the executor rides
    /// `IoDriver::diagnostics` into the snapshot's I/O-health section.
    struct Reporting<'a> {
        inner: &'a StubDriver,
        report: Mutex<Option<DriverDiagnostics>>,
    }

    impl IoDriver for Reporting<'_> {
        fn read(&self, point: PointId) -> Result<Sample, IoError> {
            self.inner.read(point)
        }

        fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
            self.inner.write(point, value)
        }

        fn diagnostics(&self) -> Option<DriverDiagnostics> {
            self.report.lock().unwrap().clone()
        }
    }

    #[test]
    fn snapshot_rides_driver_diagnostics_in_io_health() {
        let inner = StubDriver::new(&[float(10)], &[]);
        let driver = Reporting {
            inner: &inner,
            report: Mutex::new(Some(DriverDiagnostics {
                link: LinkState::Disconnected,
                last_error: Some("no live connection to the plant server".to_string()),
                exchange: None,
            })),
        };
        let map: PointMap = [(PointId(10), Direction::In, ValueKind::Float)]
            .into_iter()
            .collect();
        let mut executor = Executor::new(&driver, map, Vec::new()).unwrap();
        executor.scan();

        // The driver's transport surface reports beside the counters —
        // link health, distinct from per-point quality.
        assert_eq!(
            executor.snapshot().io_health.driver,
            Some(DriverDiagnostics {
                link: LinkState::Disconnected,
                last_error: Some("no live connection to the plant server".to_string()),
                exchange: None,
            })
        );
    }

    #[test]
    fn snapshot_reports_fed_scan_overruns() {
        let driver = StubDriver::new(&[float(10)], &[]);
        let map: PointMap = [(PointId(10), Direction::In, ValueKind::Float)]
            .into_iter()
            .collect();
        let mut executor = Executor::new(&driver, map, Vec::new()).unwrap();

        // The documented feed: the pacing shell reports each overran
        // cycle through `record_scan_overrun`; the executor only counts.
        assert_eq!(executor.snapshot().io_health.scan_overruns, 0);
        executor.record_scan_overrun();
        executor.record_scan_overrun();
        assert_eq!(executor.snapshot().io_health.scan_overruns, 2);
    }

    #[test]
    fn identical_runs_snapshot_identical_io_health() {
        // The same fault script over two equivalent runs produces the
        // same health payload — counters and attribution are
        // deterministic like the rest of the snapshot.
        let run = || {
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
                vec![Box::new(Constant {
                    name: "const",
                    output: PointId(20),
                    value: 1.0,
                })],
            )
            .unwrap();
            executor.scan();
            driver.faults.lock().unwrap().insert(PointId(10));
            executor.scan();
            executor.scan();
            executor.snapshot().io_health
        };
        let health_a = run();
        let health_b = run();
        assert_eq!(health_a, health_b);
        assert_eq!(
            serde_json::to_string(&health_a).unwrap(),
            serde_json::to_string(&health_b).unwrap()
        );
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

        executor.run(2);
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
        executor.scan();

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

    /// Setpoint rig: `Scale` reads writable `In` point 10 and drives `Out`
    /// point 20 at gain 2, plus an unwritten `Out` point 30.
    fn setpoint_rig(driver: &StubDriver) -> Executor<'_> {
        let map = PointMap::new()
            .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
            .with_point(PointId(20), Direction::Out, ValueKind::Float)
            .with_point(PointId(30), Direction::Out, ValueKind::Float);
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
    fn attributed_commands_stamp_the_receipt_without_touching_validation() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = setpoint_rig(&driver);
        let command = write_value(10, ValueKind::Float, Value::Float(5.0));

        // The declared actor rides the receipt from submission through
        // the scan boundary, untouched by validation.
        let receipt = executor.submit_command_as(command.clone(), Some("operator-7".to_string()));
        assert_eq!(
            receipt,
            CommandReceipt {
                command: command.clone(),
                outcome: CommandOutcome::Accepted {
                    apply_tick: Tick(1)
                },
                actor: Some("operator-7".to_string()),
            }
        );
        executor.scan();
        assert_eq!(
            executor.receipts().last().unwrap().actor.as_deref(),
            Some("operator-7")
        );

        // A rejected attributed command is the same rejection with the
        // actor stamped — attribution never changes validation.
        let rejected = executor.submit_command_as(
            write_value(99, ValueKind::Float, Value::Float(1.0)),
            Some("operator-7".to_string()),
        );
        assert_eq!(
            rejected.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::UnknownPoint { point: PointId(99) }
            }
        );
        assert_eq!(rejected.actor.as_deref(), Some("operator-7"));
    }

    #[test]
    fn setpoint_command_applies_at_next_scan_and_holds() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = setpoint_rig(&driver);
        let command = write_value(10, ValueKind::Float, Value::Float(5.0));

        let receipt = executor.submit_command(command.clone());
        assert_eq!(
            receipt,
            CommandReceipt {
                command,
                outcome: CommandOutcome::Accepted {
                    apply_tick: Tick(1)
                },
                actor: None,
            }
        );
        // Queued, not yet applied: the driver still holds the old value.
        assert_eq!(driver_value(&driver, 10), Value::Float(0.0));

        executor.scan();
        assert_eq!(driver_value(&driver, 20), Value::Float(10.0));
        assert_eq!(
            executor.receipts().last().unwrap().outcome,
            CommandOutcome::Applied { tick: Tick(1) }
        );

        // The write landed on the field, so the setpoint holds for later
        // scans rather than reverting.
        executor.scan();
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
        executor.scan();
        assert_eq!(executor.receipts().len(), 3);
        assert!(driver_value(&driver, 10) == Value::Float(0.0));
    }

    #[test]
    fn unwritable_points_refuse_commands_at_submission() {
        let driver = StubDriver::new(&[float(10), float(11), float(20), float(30)], &[]);
        let map = PointMap::new()
            .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
            .with_point(PointId(11), Direction::In, ValueKind::Float)
            .with_point(PointId(20), Direction::Out, ValueKind::Float)
            // The map tolerates a mark on an `Out` point — a model
            // declaring one fails validation — and the command path
            // refuses it regardless.
            .with_writable_point(PointId(30), Direction::Out, ValueKind::Float)
            .with_writable_internal(
                PointId(40),
                Direction::In,
                ValueKind::Float,
                Value::Float(1.0),
            )
            .with_internal(
                PointId(41),
                Direction::In,
                ValueKind::Float,
                Value::Float(2.0),
            );
        let mut executor = Executor::new(&driver, map, vec![]).unwrap();

        // Unmarked `In` points — field or internal — and every `Out`
        // point, marked or not, refuse at submission naming the point.
        for point in [11, 20, 30, 41] {
            let receipt =
                executor.submit_command(write_value(point, ValueKind::Float, Value::Float(9.0)));
            assert_eq!(
                receipt.outcome,
                CommandOutcome::Rejected {
                    reason: CommandError::NotWritable {
                        point: PointId(point)
                    }
                },
                "point {point}"
            );
            assert_eq!(receipt.command.point(), Some(PointId(point)));
        }

        // Writability is checked before the payload: a wrong-kind command
        // to an unmarked point still reads `NotWritable`.
        let receipt = executor.submit_command(write_value(11, ValueKind::Int, Value::Int(9)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotWritable { point: PointId(11) }
            }
        );

        // Nothing was queued: a scan adds no application receipts and
        // leaves driver and image untouched.
        executor.scan();
        assert_eq!(executor.receipts().len(), 5);
        assert_eq!(driver_value(&driver, 11), Value::Float(0.0));
        let held = executor
            .snapshot()
            .points
            .iter()
            .find(|telemetry| telemetry.point == PointId(41))
            .and_then(|telemetry| telemetry.sample)
            .unwrap();
        assert_eq!(held.value, Value::Float(2.0));
    }

    #[test]
    fn driver_rejection_is_recorded_at_the_scan_boundary() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = setpoint_rig(&driver);
        driver.faults.lock().unwrap().insert(PointId(10));

        let command = write_value(10, ValueKind::Float, Value::Float(9.0));
        let receipt = executor.submit_command(command);
        // Static checks pass — the fault only surfaces at the driver.
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(1)
            }
        );

        executor.scan();
        assert_eq!(
            executor.receipts().last().unwrap().outcome,
            CommandOutcome::Rejected {
                reason: CommandError::DriverRejected {
                    point: PointId(10),
                    error: IoError::Disconnected(PointId(10)),
                }
            }
        );
    }

    #[test]
    fn output_point_commands_are_refused_not_writable() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = setpoint_rig(&driver);

        // The recorded rule for `Out` points is rejection: a command to
        // one — component-written or not — is refused at submission and
        // never reaches the driver or the image.
        for point in [20, 30] {
            let receipt =
                executor.submit_command(write_value(point, ValueKind::Float, Value::Float(7.0)));
            assert_eq!(
                receipt.outcome,
                CommandOutcome::Rejected {
                    reason: CommandError::NotWritable {
                        point: PointId(point)
                    }
                }
            );
        }
        executor.scan();
        assert_eq!(executor.receipts().len(), 2);
        assert_eq!(driver_value(&driver, 30), Value::Float(0.0));
    }

    #[test]
    fn identical_command_runs_produce_identical_receipts() {
        let run = || {
            let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
            let mut executor = setpoint_rig(&driver);
            executor.submit_command(write_value(10, ValueKind::Float, Value::Float(5.0)));
            executor.submit_command(write_value(99, ValueKind::Float, Value::Float(0.0)));
            executor.submit_command(write_value(30, ValueKind::Float, Value::Float(7.0)));
            executor.run(2);
            executor.submit_command(write_value(10, ValueKind::Float, Value::Float(8.0)));
            executor.scan();
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
        let outcomes: Vec<CommandOutcome> = receipts
            .iter()
            .map(|receipt| receipt.outcome.clone())
            .collect();
        assert_eq!(
            outcomes,
            vec![
                CommandOutcome::Applied { tick: Tick(1) },
                CommandOutcome::Rejected {
                    reason: CommandError::UnknownPoint { point: PointId(99) }
                },
                CommandOutcome::Rejected {
                    reason: CommandError::NotWritable { point: PointId(30) }
                },
                CommandOutcome::Applied { tick: Tick(3) },
            ]
        );
    }

    fn force_point(point: u64, kind: ValueKind, value: Value) -> Command {
        Command::ForcePoint {
            point: PointId(point),
            kind,
            value,
        }
    }

    fn unforce_point(point: u64) -> Command {
        Command::UnforcePoint {
            point: PointId(point),
        }
    }

    /// The sample the scan image reports for a forced point.
    fn forced(value: Value, tick: u64) -> Sample {
        Sample::new(
            value,
            Quality::Uncertain(QualityReason::Substituted),
            Tick(tick),
        )
    }

    #[test]
    fn force_holds_across_scans_with_substituted_quality() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = setpoint_rig(&driver);

        // Baseline: the live field value reads through with Good quality.
        executor.scan();
        assert_eq!(
            executor.sample(PointId(10)),
            Some(Sample::good(Value::Float(0.0), Tick(1)))
        );

        let receipt = executor.submit_command(force_point(10, ValueKind::Float, Value::Float(5.0)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(2)
            }
        );

        // The applying scan substitutes the forced value at Substituted
        // quality and the component's step observes it the same scan.
        executor.scan();
        assert_eq!(
            executor.receipts().last().unwrap().outcome,
            CommandOutcome::Applied { tick: Tick(2) }
        );
        assert_eq!(
            executor.sample(PointId(10)),
            Some(forced(Value::Float(5.0), 2))
        );
        assert_eq!(driver_value(&driver, 20), Value::Float(10.0));
        // The field itself is untouched: a force writes nothing to the
        // driver and bypasses its reads.
        assert_eq!(driver_value(&driver, 10), Value::Float(0.0));

        // The field reasserting a different value changes nothing the
        // image reports: the force holds across scans, each stamping the
        // current tick at Substituted quality.
        driver.write(PointId(10), Value::Float(3.0)).unwrap();
        for n in 3..=5u64 {
            executor.scan();
            assert_eq!(
                executor.sample(PointId(10)),
                Some(forced(Value::Float(5.0), n)),
                "scan {n}"
            );
            assert_eq!(driver_value(&driver, 20), Value::Float(10.0));
        }
        // And the substitution reports into the snapshot the same way.
        assert_eq!(
            executor.snapshot().points[0].sample,
            Some(forced(Value::Float(5.0), 5))
        );
    }

    #[test]
    fn unforce_resumes_driver_reads_at_the_scan_boundary() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = setpoint_rig(&driver);
        executor.submit_command(force_point(10, ValueKind::Float, Value::Float(5.0)));
        executor.scan();
        // The field moves while the force stands; the image still holds
        // the forced value.
        driver.write(PointId(10), Value::Float(2.0)).unwrap();
        executor.scan();
        assert_eq!(
            executor.sample(PointId(10)),
            Some(forced(Value::Float(5.0), 2))
        );

        let receipt = executor.submit_command(unforce_point(10));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(3)
            }
        );

        // The release applies at the head of the scan, so that scan's
        // input phase already reads the field again — the documented
        // boundary — and the component sees the live value the same scan.
        executor.scan();
        assert_eq!(
            executor.receipts().last().unwrap().outcome,
            CommandOutcome::Applied { tick: Tick(3) }
        );
        assert_eq!(
            executor.sample(PointId(10)),
            Some(Sample::good(Value::Float(2.0), Tick(3)))
        );
        assert_eq!(driver_value(&driver, 20), Value::Float(4.0));
        assert!(executor.snapshot().forces.is_empty());
    }

    #[test]
    fn force_commands_reject_outside_the_writable_surface() {
        let driver = StubDriver::new(&[float(10), float(11), float(20), float(30)], &[]);
        let map = PointMap::new()
            .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
            .with_point(PointId(11), Direction::In, ValueKind::Float)
            .with_point(PointId(20), Direction::Out, ValueKind::Float)
            // A map may carry the mark on an `Out` point — a model
            // declaring one fails validation — and the command path
            // refuses it regardless.
            .with_writable_point(PointId(30), Direction::Out, ValueKind::Float);
        let mut executor = Executor::new(&driver, map, vec![]).unwrap();

        // Unknown point.
        let receipt = executor.submit_command(force_point(99, ValueKind::Float, Value::Float(1.0)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::UnknownPoint { point: PointId(99) }
            }
        );

        // Unmarked `In` point and every `Out` point, marked or not.
        for point in [11, 20, 30] {
            let receipt =
                executor.submit_command(force_point(point, ValueKind::Float, Value::Float(1.0)));
            assert_eq!(
                receipt.outcome,
                CommandOutcome::Rejected {
                    reason: CommandError::NotWritable {
                        point: PointId(point)
                    }
                },
                "point {point}"
            );
        }

        // Declared kind disagrees with the map's, then the value's
        // variant with the declared kind.
        let receipt = executor.submit_command(force_point(10, ValueKind::Int, Value::Int(1)));
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
        let receipt = executor.submit_command(force_point(10, ValueKind::Float, Value::Bool(true)));
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

        // Unforce shares the surface: unknown and unwritable points
        // refuse with the same named reasons.
        let receipt = executor.submit_command(unforce_point(99));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::UnknownPoint { point: PointId(99) }
            }
        );
        for point in [11, 20, 30] {
            let receipt = executor.submit_command(unforce_point(point));
            assert_eq!(
                receipt.outcome,
                CommandOutcome::Rejected {
                    reason: CommandError::NotWritable {
                        point: PointId(point)
                    }
                },
                "point {point}"
            );
        }

        // Every rejection was at submission: a scan queues nothing.
        executor.scan();
        assert_eq!(executor.receipts().len(), 10);
        assert_eq!(driver_value(&driver, 10), Value::Float(0.0));
    }

    #[test]
    fn unforce_without_a_force_applies_as_a_noop() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = setpoint_rig(&driver);

        // Releasing an unforced point still applies — the release is
        // idempotent — and the scan reads the field as usual.
        let receipt = executor.submit_command(unforce_point(10));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(1)
            }
        );
        executor.scan();
        assert_eq!(
            executor.receipts().last().unwrap().outcome,
            CommandOutcome::Applied { tick: Tick(1) }
        );
        assert_eq!(
            executor.sample(PointId(10)),
            Some(Sample::good(Value::Float(0.0), Tick(1)))
        );
        assert!(executor.snapshot().forces.is_empty());
    }

    #[test]
    fn reforce_repins_the_value_and_write_still_reaches_the_field() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = setpoint_rig(&driver);
        executor.submit_command(force_point(10, ValueKind::Float, Value::Float(5.0)));
        executor.scan();

        // A second force replaces the first at its boundary.
        executor.submit_command(force_point(10, ValueKind::Float, Value::Float(9.0)));
        executor.scan();
        assert_eq!(
            executor.sample(PointId(10)),
            Some(forced(Value::Float(9.0), 2))
        );

        // A write to a forced field point still reaches the driver — the
        // force overrides the image, not the field — so the release
        // observes the field's current value.
        executor.submit_command(write_value(10, ValueKind::Float, Value::Float(4.0)));
        executor.scan();
        assert_eq!(driver_value(&driver, 10), Value::Float(4.0));
        assert_eq!(
            executor.sample(PointId(10)),
            Some(forced(Value::Float(9.0), 3))
        );

        executor.submit_command(unforce_point(10));
        executor.scan();
        assert_eq!(
            executor.sample(PointId(10)),
            Some(Sample::good(Value::Float(4.0), Tick(4)))
        );
    }

    #[test]
    fn forced_internal_point_reports_the_substituted_value() {
        // A writable internal `In` point is on the same surface: its
        // held value is already operator-owned, so the force's visible
        // effect is the Substituted stamp each scan.
        let driver = StubDriver::new(&[], &[]);
        let mut executor = internal_rig(&driver);
        executor.submit_command(Command::ForcePoint {
            point: PointId(10),
            kind: ValueKind::Float,
            value: Value::Float(8.0),
        });
        executor.scan();
        executor.scan();
        assert_eq!(
            executor.sample(PointId(10)),
            Some(forced(Value::Float(8.0), 2))
        );

        executor.submit_command(Command::UnforcePoint { point: PointId(10) });
        executor.scan();
        // Release resumes the held-value rule: the image keeps the last
        // sample's value — the forced value as last stamped while
        // forced — re-stamped `Good` at the release boundary rather than
        // left claiming substituted data.
        assert_eq!(
            executor.sample(PointId(10)),
            Some(Sample::good(Value::Float(8.0), Tick(3)))
        );
        assert!(executor.snapshot().forces.is_empty());
    }

    #[test]
    fn unforce_restamps_an_internal_held_value_good() {
        // The release of a forced internal writable point leaves the
        // held value stamped `Good` at the release boundary — not the
        // force's `Substituted` mark, which claims a substitution no
        // longer standing — and a same-value `WriteValue` produces the
        // identical observable state.
        let driver = StubDriver::new(&[], &[]);
        let mut executor = internal_rig(&driver);
        executor.submit_command(force_point(10, ValueKind::Float, Value::Float(8.0)));
        executor.run(2);
        assert_eq!(
            executor.sample(PointId(10)),
            Some(forced(Value::Float(8.0), 2))
        );

        executor.submit_command(unforce_point(10));
        executor.scan();
        assert_eq!(
            executor.receipts().last().unwrap().outcome,
            CommandOutcome::Applied { tick: Tick(3) }
        );
        assert_eq!(
            executor.sample(PointId(10)),
            Some(Sample::good(Value::Float(8.0), Tick(3)))
        );
        assert!(executor.snapshot().forces.is_empty());
        // The release boundary's image — the held point and the
        // downstream internal `Out` the component derived from it.
        let released = [PointId(10), PointId(20)].map(|point| executor.sample(point));

        // The restamp is the held value now: later scans leave it
        // untouched rather than re-substituting.
        executor.scan();
        assert_eq!(
            executor.sample(PointId(10)),
            Some(Sample::good(Value::Float(8.0), Tick(3)))
        );

        // A parallel run writing the same value at the same boundary
        // lands on the identical image.
        let written_driver = StubDriver::new(&[], &[]);
        let mut written = internal_rig(&written_driver);
        written.run(2);
        written.submit_command(write_value(10, ValueKind::Float, Value::Float(8.0)));
        written.scan();
        for (sample, point) in released.into_iter().zip([PointId(10), PointId(20)]) {
            assert_eq!(written.sample(point), sample, "point {point:?}");
        }
    }

    /// QA finding
    /// `write-to-forced-held-point-settles-applied-without-effect`: an
    /// internal `In` point's image IS its store, so a write staged
    /// while a force stands is overwritten by the force's input-phase
    /// substitution in the same scan — an `Applied` receipt would
    /// journal an effect that never lands. The receipted path refuses
    /// the write by name instead, and the image, the receipt, and the
    /// post-release value all agree.
    #[test]
    fn write_to_a_forced_internal_point_is_refused_by_name() {
        let driver = StubDriver::new(&[], &[]);
        let mut executor = internal_rig(&driver);

        executor.scan();
        // The held point keeps its wired initial: internal `In` samples
        // re-stamp only when a command writes them.
        assert_eq!(
            executor.sample(PointId(10)),
            Some(Sample::good(Value::Float(2.5), Tick::ZERO))
        );

        executor.submit_command(force_point(10, ValueKind::Float, Value::Float(8.0)));
        executor.scan();
        assert_eq!(
            executor.receipts().last().unwrap().outcome,
            CommandOutcome::Applied { tick: Tick(2) }
        );
        assert_eq!(
            executor.sample(PointId(10)),
            Some(forced(Value::Float(8.0), 2))
        );

        // The write refuses at submission with the named reason — never
        // accepted, never queued, never applied.
        let receipt = executor.submit_command(write_value(10, ValueKind::Float, Value::Float(4.0)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::PointForced { point: PointId(10) }
            }
        );

        // The next scan substitutes the forced value as usual: the
        // written 4.0 enters no publication, and the settled receipt
        // says exactly that — `Rejected`, not a phantom `Applied`.
        executor.scan();
        assert_eq!(
            executor.sample(PointId(10)),
            Some(forced(Value::Float(8.0), 3))
        );
        assert_eq!(
            executor.receipts().last().unwrap().outcome,
            CommandOutcome::Rejected {
                reason: CommandError::PointForced { point: PointId(10) }
            }
        );

        // Release stands the forced value `Good` — the only value the
        // applied commands ever staged.
        executor.submit_command(unforce_point(10));
        executor.scan();
        assert_eq!(
            executor.sample(PointId(10)),
            Some(Sample::good(Value::Float(8.0), Tick(4)))
        );
        executor.scan();
        assert_eq!(
            executor.sample(PointId(10)),
            Some(Sample::good(Value::Float(8.0), Tick(4)))
        );
    }

    #[test]
    fn write_queued_behind_a_force_in_the_same_boundary_is_refused() {
        // The boundary re-validates each queued command in submission
        // order, so a write accepted while the point was unforced still
        // settles `Rejected` when a force queued ahead of it lands
        // first — the queue's own ordering can newly answer the
        // run-state check.
        let driver = StubDriver::new(&[], &[]);
        let mut executor = internal_rig(&driver);

        executor.submit_command(force_point(10, ValueKind::Float, Value::Float(8.0)));
        let receipt = executor.submit_command(write_value(10, ValueKind::Float, Value::Float(4.0)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(1)
            }
        );

        executor.scan();
        let outcomes: Vec<&CommandOutcome> = executor
            .receipts()
            .iter()
            .map(|receipt| &receipt.outcome)
            .collect();
        assert_eq!(
            outcomes,
            vec![
                &CommandOutcome::Applied { tick: Tick(1) },
                &CommandOutcome::Rejected {
                    reason: CommandError::PointForced { point: PointId(10) }
                },
            ]
        );
        assert_eq!(
            executor.sample(PointId(10)),
            Some(forced(Value::Float(8.0), 1))
        );

        // Submission validates against the standing force set, not the
        // pending queue: a write submitted while the force still stands
        // refuses even with its release already queued behind it.
        executor.submit_command(unforce_point(10));
        let refused = executor.submit_command(write_value(10, ValueKind::Float, Value::Float(4.0)));
        assert_eq!(
            refused.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::PointForced { point: PointId(10) }
            }
        );

        // Once the release lands the same write applies legitimately —
        // the point is no longer forced when the write resolves, so its
        // value lands in the image and holds.
        executor.scan();
        executor.submit_command(write_value(10, ValueKind::Float, Value::Float(4.0)));
        executor.scan();
        assert_eq!(
            executor.receipts().last().unwrap().outcome,
            CommandOutcome::Applied { tick: Tick(3) }
        );
        assert_eq!(
            executor.sample(PointId(10)),
            Some(Sample::good(Value::Float(4.0), Tick(3)))
        );
    }

    #[test]
    fn force_overrides_an_internal_link_route() {
        // A link-driven internal `In` point marked writable can be
        // forced: the force wins over the route — it is the operator's
        // override of every channel feeding the image.
        let driver = StubDriver::new(&[float(40)], &[]);
        let map = PointMap::new()
            .with_internal(
                PointId(20),
                Direction::Out,
                ValueKind::Float,
                Value::Float(0.0),
            )
            .with_writable_internal(
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
            vec![Box::new(Constant {
                name: "producer",
                output: PointId(20),
                value: 9.0,
            })],
        )
        .unwrap();
        executor.scan();

        executor.submit_command(force_point(30, ValueKind::Float, Value::Float(7.0)));
        executor.scan();
        executor.scan();
        assert_eq!(
            executor.sample(PointId(30)),
            Some(forced(Value::Float(7.0), 3))
        );

        executor.submit_command(unforce_point(30));
        executor.scan();
        // The route resumes at the release boundary.
        assert_eq!(
            executor.sample(PointId(30)),
            Some(Sample::good(Value::Float(9.0), Tick(4)))
        );
    }

    #[test]
    fn forced_field_input_reports_no_failed_read_while_forced() {
        // The driver read is bypassed: a field fault on a forced point
        // produces no failed-read count and the forced sample stands.
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = setpoint_rig(&driver);
        executor.submit_command(force_point(10, ValueKind::Float, Value::Float(5.0)));
        executor.scan();

        driver.faults.lock().unwrap().insert(PointId(10));
        executor.scan();
        assert_eq!(
            executor.sample(PointId(10)),
            Some(forced(Value::Float(5.0), 2))
        );
        assert_eq!(executor.snapshot().io_health.failed_reads, 0);

        // Release resumes reads — and the fault now reports.
        executor.submit_command(unforce_point(10));
        executor.scan();
        let health = &executor.snapshot().io_health;
        assert_eq!(health.failed_reads, 1);
        assert_eq!(
            executor.sample(PointId(10)).unwrap().quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
    }

    #[test]
    fn snapshot_lists_the_force_set() {
        let driver = StubDriver::new(&[float(10), float(11), float(20)], &[]);
        let map = PointMap::new()
            .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
            .with_writable_point(PointId(11), Direction::In, ValueKind::Bool)
            .with_point(PointId(20), Direction::Out, ValueKind::Float);
        let mut executor = Executor::new(&driver, map, vec![]).unwrap();
        assert!(executor.snapshot().forces.is_empty());

        // Submit out of id order: the snapshot lists the set in
        // ascending point order regardless.
        executor.submit_command(Command::ForcePoint {
            point: PointId(11),
            kind: ValueKind::Bool,
            value: Value::Bool(true),
        });
        executor.submit_command(force_point(10, ValueKind::Float, Value::Float(5.0)));
        executor.scan();
        assert_eq!(
            executor.snapshot().forces,
            vec![
                dcs_core::ForcedPoint {
                    point: PointId(10),
                    value: Value::Float(5.0),
                },
                dcs_core::ForcedPoint {
                    point: PointId(11),
                    value: Value::Bool(true),
                },
            ]
        );

        executor.submit_command(unforce_point(10));
        executor.scan();
        assert_eq!(
            executor.snapshot().forces,
            vec![dcs_core::ForcedPoint {
                point: PointId(11),
                value: Value::Bool(true),
            }]
        );
    }

    /// Rig for forcing checkpoint tests: `Scale` reads writable `In`
    /// point 10 onto `Out` 20 at gain 2.
    fn forcing_checkpoint_components() -> Vec<Box<dyn Component>> {
        vec![Box::new(Scale {
            name: "a",
            input: PointId(10),
            output: PointId(20),
            gain: 2.0,
        })]
    }

    fn forcing_checkpoint_map() -> PointMap {
        PointMap::new()
            .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
            .with_point(PointId(20), Direction::Out, ValueKind::Float)
    }

    #[test]
    fn checkpoint_roundtrip_preserves_the_force_set() {
        let driver = StubDriver::new(&[float(10), float(20)], &[]);
        let mut executor = Executor::new(
            &driver,
            forcing_checkpoint_map(),
            forcing_checkpoint_components(),
        )
        .unwrap();
        executor.submit_command(force_point(10, ValueKind::Float, Value::Float(5.0)));
        executor.run(2);

        let checkpoint = executor.checkpoint();
        assert_eq!(
            checkpoint.forces,
            BTreeMap::from([(PointId(10), Value::Float(5.0))])
        );
        // The force set rides the serialized form.
        let json = serde_json::to_string(&checkpoint).unwrap();
        let checkpoint: Checkpoint = serde_json::from_str(&json).unwrap();
        assert_eq!(checkpoint.forces[&PointId(10)], Value::Float(5.0));

        // A fresh executor — its own driver reporting the field's real
        // value — restores the force and keeps substituting.
        let standby = StubDriver::new(&[float(10), float(20)], &[]);
        standby.write(PointId(10), Value::Float(3.0)).unwrap();
        let mut restored = Executor::restore(
            &standby,
            forcing_checkpoint_map(),
            forcing_checkpoint_components(),
            &checkpoint,
            None,
        )
        .unwrap();
        restored.scan();
        assert_eq!(
            restored.sample(PointId(10)),
            Some(forced(Value::Float(5.0), 3))
        );
        assert_eq!(driver_value(&standby, 20), Value::Float(10.0));
        assert_eq!(
            restored.snapshot().forces,
            vec![dcs_core::ForcedPoint {
                point: PointId(10),
                value: Value::Float(5.0),
            }]
        );
    }

    #[test]
    fn apply_aligns_the_force_set() {
        // The running-standby half: `apply` converges a live executor's
        // force set to the checkpoint's — forcing what the active forces
        // and releasing what it released.
        let active_driver = StubDriver::new(&[float(10), float(20)], &[]);
        let mut active = Executor::new(
            &active_driver,
            forcing_checkpoint_map(),
            forcing_checkpoint_components(),
        )
        .unwrap();
        active.submit_command(force_point(10, ValueKind::Float, Value::Float(5.0)));
        active.scan();

        let standby_driver = StubDriver::new(&[float(10), float(20)], &[]);
        standby_driver
            .write(PointId(10), Value::Float(1.0))
            .unwrap();
        let mut standby = Executor::new(
            &standby_driver,
            forcing_checkpoint_map(),
            forcing_checkpoint_components(),
        )
        .unwrap();
        standby.apply(&active.checkpoint()).unwrap();
        standby.scan();
        assert_eq!(
            standby.sample(PointId(10)),
            Some(forced(Value::Float(5.0), 2))
        );

        // A later checkpoint whose force set is empty releases the
        // standby's force: the checkpoint's set is authoritative.
        active.submit_command(unforce_point(10));
        active.scan();
        standby.apply(&active.checkpoint()).unwrap();
        standby.scan();
        assert_eq!(
            standby.sample(PointId(10)),
            Some(Sample::good(Value::Float(1.0), Tick(3)))
        );
        assert!(standby.snapshot().forces.is_empty());
    }

    #[test]
    fn checkpoint_carries_the_command_receipt_log() {
        // The run's command audit is run state like the image's: the
        // checkpoint carries the receipt log so the pair presents one
        // `GET /receipts` answer whichever peer serves it.
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = setpoint_rig(&driver);
        executor.submit_command_as(
            write_value(10, ValueKind::Float, Value::Float(5.0)),
            Some("operator-7".to_string()),
        );
        executor.scan();

        let checkpoint = executor.checkpoint();
        assert_eq!(checkpoint.receipts, executor.receipts());

        // The section rides the serialized form, and a checkpoint
        // written before it existed restores as an empty log.
        let json = serde_json::to_string(&checkpoint).unwrap();
        let roundtrip: Checkpoint = serde_json::from_str(&json).unwrap();
        assert_eq!(roundtrip.receipts, executor.receipts());
        let mut legacy = serde_json::from_str::<serde_json::Value>(&json).unwrap();
        legacy.as_object_mut().unwrap().remove("receipts");
        let legacy: Checkpoint = serde_json::from_value(legacy).unwrap();
        assert!(legacy.receipts.is_empty());
    }

    #[test]
    fn apply_converges_the_receipt_log_and_requeues_accepted() {
        // The running standby's half: `apply` converges the receipt log
        // to the checkpoint's — the pair's one command audit — and
        // re-queues a command captured still-`Accepted` between its
        // submission boundary and its applying scan, so a takeover
        // mid-flight never drops it.
        let active_driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut active = setpoint_rig(&active_driver);
        active.submit_command_as(
            write_value(10, ValueKind::Float, Value::Float(5.0)),
            Some("operator-7".to_string()),
        );
        active.scan();
        // The second command is checkpointed still-`Accepted`: submitted
        // after the settling scan, captured before its own boundary.
        active.submit_command(write_value(10, ValueKind::Float, Value::Float(7.0)));
        let checkpoint = active.checkpoint();

        let standby_driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut standby = setpoint_rig(&standby_driver);
        standby.apply(&checkpoint).unwrap();
        assert_eq!(standby.receipts(), active.receipts());

        // The re-queued command lands at the standby's next boundary
        // exactly as the active's would have landed it.
        standby.scan();
        assert_eq!(
            standby.receipts().last().unwrap().outcome,
            CommandOutcome::Applied { tick: Tick(2) }
        );
        assert_eq!(driver_value(&standby_driver, 20), Value::Float(14.0));

        // The cold-start half adopts the same log.
        let restored_driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let restored = Executor::restore(
            &restored_driver,
            PointMap::new()
                .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
                .with_point(PointId(20), Direction::Out, ValueKind::Float)
                .with_point(PointId(30), Direction::Out, ValueKind::Float),
            vec![Box::new(Scale {
                name: "a",
                input: PointId(10),
                output: PointId(20),
                gain: 2.0,
            })],
            &checkpoint,
            None,
        )
        .unwrap();
        assert_eq!(restored.receipts(), active.receipts());
    }

    #[test]
    fn a_full_command_queue_refuses_admission_until_a_scan_drains_it() {
        // The bounded-ingress bound: at capacity a validated command is
        // refused with the named `queue_full` rejection — nothing queues
        // — and the next scan's drain re-opens admission.
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = setpoint_rig(&driver).with_command_queue_capacity(2);
        assert_eq!(executor.command_queue_capacity(), 2);

        // Fill the queue to the declared bound.
        for value in [3.0, 4.0] {
            let receipt =
                executor.submit_command(write_value(10, ValueKind::Float, Value::Float(value)));
            assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        }
        assert_eq!(executor.snapshot().command_queue.depth, 2);

        // Past the bound: the named rejection, nothing queued.
        let receipt = executor.submit_command(write_value(10, ValueKind::Float, Value::Float(5.0)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::QueueFull {
                    point: Some(PointId(10)),
                    capacity: 2,
                }
            }
        );
        assert_eq!(executor.receipts().len(), 3);
        assert_eq!(executor.snapshot().command_queue.depth, 2);

        // The scan drains the queue: the queued commands still apply in
        // submission order and settle their receipts.
        executor.scan();
        assert_eq!(
            executor.receipts()[0].outcome,
            CommandOutcome::Applied { tick: Tick(1) }
        );
        assert_eq!(
            executor.receipts()[1].outcome,
            CommandOutcome::Applied { tick: Tick(1) }
        );
        assert_eq!(driver_value(&driver, 10), Value::Float(4.0));
        assert_eq!(executor.snapshot().command_queue.depth, 0);

        // Admission succeeds again once a scan drained the queue.
        let receipt = executor.submit_command(write_value(10, ValueKind::Float, Value::Float(6.0)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(2)
            }
        );
    }

    #[test]
    fn the_receipt_log_evicts_oldest_settled_entries_at_its_bound() {
        // The bounded audit: past the declared capacity the leading
        // settled receipts evict oldest-first — the log, the checkpoint
        // section it rides, and the served `GET /receipts` answer all
        // stay flat in lifetime command count, while `attempts` keeps
        // the lifetime count the eviction gap reads from.
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = setpoint_rig(&driver).with_receipt_log_capacity(4);
        assert_eq!(executor.receipt_log_capacity(), 4);

        for _ in 0..6 {
            executor.submit_command(write_value(10, ValueKind::Float, Value::Float(5.0)));
            executor.scan();
        }

        // Six submissions, four retained — the newest four, settled at
        // their own ticks.
        assert_eq!(executor.receipts().len(), 4);
        assert_eq!(executor.receipt_base(), 2);
        assert_eq!(executor.snapshot().command_queue.attempts, 6);
        for (offset, receipt) in executor.receipts().iter().enumerate() {
            assert_eq!(
                receipt.outcome,
                CommandOutcome::Applied {
                    tick: Tick(3 + offset as u64)
                }
            );
        }

        // The checkpoint carries the bounded window — its serialized
        // size stays flat in lifetime command count. (Same-width ticks
        // either side keep the comparison honest: every field that can
        // still change digits — tick, attempts, sample stamps — does so
        // within one width here.)
        for _ in 0..100 {
            executor.submit_command(write_value(10, ValueKind::Float, Value::Float(5.0)));
            executor.scan();
        }
        let first = serde_json::to_string(&executor.checkpoint()).unwrap().len();
        for _ in 0..4 {
            executor.submit_command(write_value(10, ValueKind::Float, Value::Float(5.0)));
            executor.scan();
        }
        assert_eq!(executor.receipts().len(), 4);
        assert_eq!(executor.receipt_base(), 106);
        assert_eq!(executor.snapshot().command_queue.attempts, 110);
        let checkpoint = executor.checkpoint();
        assert_eq!(checkpoint.receipts.len(), 4);
        assert_eq!(checkpoint.receipt_base(), 106);
        assert_eq!(serde_json::to_string(&checkpoint).unwrap().len(), first);
    }

    #[test]
    fn pending_receipts_are_never_evicted() {
        // Pending commands are run state, not retention: a log past its
        // bound holding `Accepted` entries keeps every one, and they
        // still settle at their boundary — eviction resumes on the
        // settled prefix once they do.
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = setpoint_rig(&driver)
            .with_command_queue_capacity(6)
            .with_receipt_log_capacity(2);

        for value in 0..6 {
            let receipt = executor.submit_command(write_value(
                10,
                ValueKind::Float,
                Value::Float(value as f64),
            ));
            assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        }
        // Six pending past the bound of two — all retained, none evicted.
        assert_eq!(executor.receipts().len(), 6);
        assert_eq!(executor.receipt_base(), 0);

        executor.scan();
        for receipt in executor.receipts() {
            assert_eq!(receipt.outcome, CommandOutcome::Applied { tick: Tick(1) });
        }
        assert_eq!(driver_value(&driver, 10), Value::Float(5.0));

        // Settled now: the next submission evicts the settled prefix to
        // the bound — five evict, leaving one settled plus the pending.
        executor.submit_command(write_value(10, ValueKind::Float, Value::Float(9.0)));
        assert_eq!(executor.receipts().len(), 2);
        assert_eq!(executor.receipt_base(), 5);

        // The pending entry still indexes the log correctly through the
        // drain: it settles at its boundary, applied like the rest.
        executor.scan();
        assert_eq!(
            executor.receipts().last().unwrap().outcome,
            CommandOutcome::Applied { tick: Tick(2) }
        );
        assert_eq!(driver_value(&driver, 10), Value::Float(9.0));
    }

    #[test]
    fn carry_pending_commands_uses_absolute_indices_across_evicted_windows() {
        // Both peers' logs evict: the stale-checkpoint carry overlaps by
        // absolute submission index, so only the genuinely-new tail
        // lands — entries the source already evicted stay evicted — and
        // its `Accepted` entries re-queue for the next boundary.
        let active_driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut active = setpoint_rig(&active_driver).with_receipt_log_capacity(4);
        for value in [1.0, 2.0] {
            active.submit_command(write_value(10, ValueKind::Float, Value::Float(value)));
            active.scan();
        }
        let early = active.checkpoint();

        let standby_driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut standby = setpoint_rig(&standby_driver).with_receipt_log_capacity(4);
        standby.apply(&early).unwrap();
        assert_eq!(standby.receipts().len(), 2);

        // The active runs on: four more submissions evict its first two
        // receipts, the last checkpointed still `Accepted`.
        for value in [3.0, 4.0, 5.0] {
            active.submit_command(write_value(10, ValueKind::Float, Value::Float(value)));
            active.scan();
        }
        active.submit_command(write_value(10, ValueKind::Float, Value::Float(6.0)));
        let checkpoint = active.checkpoint();
        assert_eq!(checkpoint.receipts.len(), 4);
        assert_eq!(checkpoint.receipt_base(), 2);

        standby.carry_pending_commands(&checkpoint);
        // The union — this run's [0,2) plus the checkpoint's [2,6) —
        // trims to the bound, converging the window exactly.
        assert_eq!(standby.receipts().len(), 4);
        assert_eq!(standby.receipt_base(), 2);
        assert_eq!(standby.receipts(), checkpoint.receipts.as_slice());

        // The adopted `Accepted` entry queued past the admission bound
        // and settles at the standby's own next boundary.
        assert_eq!(standby.snapshot().command_queue.depth, 1);
        standby.scan();
        assert_eq!(
            standby.receipts().last().unwrap().outcome,
            CommandOutcome::Applied { tick: Tick(3) }
        );
        assert_eq!(driver_value(&standby_driver, 10), Value::Float(6.0));
    }

    #[test]
    fn queue_full_rejection_carries_the_actor_and_follows_validation() {
        // The admission refusal composes with attribution, and the
        // existing validation order is unchanged: a statically invalid
        // command takes its own named rejection even on a full queue.
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = tuning_rig(&driver).with_command_queue_capacity(1);
        executor.submit_command(write_value(10, ValueKind::Float, Value::Float(1.0)));

        // An invalid command on a full queue gets its own named
        // rejection — validation precedes admission.
        let receipt = executor.submit_command_as(
            write_value(99, ValueKind::Float, Value::Float(1.0)),
            Some("operator-7".to_string()),
        );
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::UnknownPoint { point: PointId(99) }
            }
        );
        assert_eq!(receipt.actor.as_deref(), Some("operator-7"));

        // A valid point command past the bound: `queue_full` naming the
        // target point, attributed like every other rejection, and
        // joining the receipt log's audit.
        let receipt = executor.submit_command_as(
            write_value(10, ValueKind::Float, Value::Float(2.0)),
            Some("operator-7".to_string()),
        );
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::QueueFull {
                    point: Some(PointId(10)),
                    capacity: 1,
                }
            }
        );
        assert_eq!(receipt.actor.as_deref(), Some("operator-7"));
        assert_eq!(executor.receipts().last().unwrap(), &receipt);

        // A valid parameter command names no point target.
        let receipt = executor.submit_command(set_parameter("loop", "gain", Value::Float(3.0)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::QueueFull {
                    point: None,
                    capacity: 1,
                }
            }
        );
    }

    #[test]
    fn restored_queue_at_capacity_admits_nothing_until_it_drains() {
        // Checkpoint interplay: pending commands are run state, adopted
        // verbatim past the bound — they still settle at their boundary
        // while new admissions refuse until a scan drains the queue.
        let active_driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut active = setpoint_rig(&active_driver);
        active.submit_command(write_value(10, ValueKind::Float, Value::Float(3.0)));
        active.submit_command(write_value(10, ValueKind::Float, Value::Float(4.0)));
        let checkpoint = active.checkpoint();

        // A standby restoring under a tighter bound (1 < 2 pending).
        let standby_driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut standby = Executor::restore(
            &standby_driver,
            PointMap::new()
                .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
                .with_point(PointId(20), Direction::Out, ValueKind::Float)
                .with_point(PointId(30), Direction::Out, ValueKind::Float),
            vec![Box::new(Scale {
                name: "a",
                input: PointId(10),
                output: PointId(20),
                gain: 2.0,
            })],
            &checkpoint,
            None,
        )
        .unwrap()
        .with_command_queue_capacity(1);
        assert_eq!(standby.snapshot().command_queue.depth, 2);

        // Over the bound: new admissions refuse until the carried set
        // drains.
        let receipt = standby.submit_command(write_value(10, ValueKind::Float, Value::Float(5.0)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::QueueFull {
                    point: Some(PointId(10)),
                    capacity: 1,
                }
            }
        );

        // The carried entries still settle at their boundary, in
        // submission order.
        standby.scan();
        assert_eq!(
            standby.receipts()[0].outcome,
            CommandOutcome::Applied { tick: Tick(1) }
        );
        assert_eq!(
            standby.receipts()[1].outcome,
            CommandOutcome::Applied { tick: Tick(1) }
        );
        assert_eq!(driver_value(&standby_driver, 10), Value::Float(4.0));

        // Drained below the bound: admission succeeds again.
        let receipt = standby.submit_command(write_value(10, ValueKind::Float, Value::Float(6.0)));
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));

        // `apply` — the running standby's half — adopts the pending set
        // under the same rule.
        let tracking_driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut tracking = setpoint_rig(&tracking_driver).with_command_queue_capacity(1);
        tracking.apply(&checkpoint).unwrap();
        assert_eq!(tracking.snapshot().command_queue.depth, 2);
        assert!(matches!(
            tracking
                .submit_command(write_value(10, ValueKind::Float, Value::Float(5.0)))
                .outcome,
            CommandOutcome::Rejected {
                reason: CommandError::QueueFull { .. }
            }
        ));
        tracking.scan();
        assert_eq!(driver_value(&tracking_driver, 10), Value::Float(4.0));
    }

    #[test]
    fn command_queue_metrics_count_attempts_rejections_and_depth() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = setpoint_rig(&driver).with_command_queue_capacity(2);
        assert_eq!(
            executor.snapshot().command_queue,
            CommandQueueDiagnostics {
                attempts: 0,
                full_rejections: 0,
                capacity: 2,
                depth: 0,
                high_water: 0,
            }
        );

        // Two accepted submissions fill the queue; a validation refusal
        // and a full-queue refusal each still count as attempts.
        for value in [3.0, 4.0] {
            executor.submit_command(write_value(10, ValueKind::Float, Value::Float(value)));
        }
        executor.submit_command(write_value(99, ValueKind::Float, Value::Float(1.0)));
        executor.submit_command(write_value(10, ValueKind::Float, Value::Float(5.0)));
        assert_eq!(
            executor.snapshot().command_queue,
            CommandQueueDiagnostics {
                attempts: 4,
                full_rejections: 1,
                capacity: 2,
                depth: 2,
                high_water: 2,
            }
        );

        // The drain clears the depth; the counters and the high-water
        // persist across it.
        executor.scan();
        executor.submit_command(write_value(10, ValueKind::Float, Value::Float(6.0)));
        assert_eq!(
            executor.snapshot().command_queue,
            CommandQueueDiagnostics {
                attempts: 5,
                full_rejections: 1,
                capacity: 2,
                depth: 1,
                high_water: 2,
            }
        );
    }

    #[test]
    fn checkpoint_rejects_a_force_on_an_unforceable_point() {
        // A checkpoint naming a forced point this map does not serve as
        // a writable `In` — or whose kind disagrees — is a different
        // mapping's artifact and fails by name.
        let driver = StubDriver::new(&[float(10), float(11), float(20)], &[]);
        let mut executor = Executor::new(
            &driver,
            forcing_checkpoint_map(),
            forcing_checkpoint_components(),
        )
        .unwrap();
        executor.submit_command(force_point(10, ValueKind::Float, Value::Float(5.0)));
        executor.scan();
        let checkpoint = executor.checkpoint();

        let unforceable_map = PointMap::new()
            .with_point(PointId(10), Direction::In, ValueKind::Float)
            .with_point(PointId(20), Direction::Out, ValueKind::Float);
        let error = Executor::restore(
            &driver,
            unforceable_map,
            forcing_checkpoint_components(),
            &checkpoint,
            None,
        )
        .unwrap_err();
        assert_eq!(error, RestoreError::UnknownForce { point: PointId(10) });

        let mut wrong_kind = checkpoint.clone();
        wrong_kind.forces.insert(PointId(10), Value::Int(5));
        let error = Executor::restore(
            &driver,
            forcing_checkpoint_map(),
            forcing_checkpoint_components(),
            &wrong_kind,
            None,
        )
        .unwrap_err();
        assert_eq!(
            error,
            RestoreError::IncompatibleForce {
                point: PointId(10),
                expected: ValueKind::Float,
                found: Value::Int(5),
            }
        );
    }

    #[test]
    fn identical_forced_runs_are_deterministic() {
        let run = || {
            let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
            let mut executor = setpoint_rig(&driver);
            executor.submit_command(force_point(10, ValueKind::Float, Value::Float(5.0)));
            executor.run(3);
            driver.write(PointId(10), Value::Float(3.0)).unwrap();
            executor.run(2);
            executor.submit_command(unforce_point(10));
            executor.scan();
            (
                executor.receipts().to_vec(),
                serde_json::to_string(&executor.snapshot()).unwrap(),
                serde_json::to_string(&executor.checkpoint()).unwrap(),
            )
        };
        assert_eq!(run(), run());
    }

    /// A component whose `gain` and `limit` are operator-tunable
    /// parameters: `out` is `in * gain` clamped to `±limit`. The hook
    /// owns the cross-parameter invariant `gain <= limit` — declared
    /// ranges alone cannot express it — and the tuned pair rides the
    /// checkpoint.
    struct Tunable {
        name: &'static str,
        input: PointId,
        output: PointId,
        gain: f64,
        limit: f64,
    }

    impl Component for Tunable {
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
            io.write_typed(
                self.output,
                (sample.value * self.gain).clamp(-self.limit, self.limit),
            )?;
            Ok(())
        }

        fn describe(&self) -> ComponentDescriptor {
            let range = ParameterRange {
                min: Value::Float(0.0),
                max: Value::Float(10.0),
            };
            ComponentDescriptor {
                name: self.name.to_string(),
                kind: "tunable".to_string(),
                label: self.name.to_string(),
                ports: self
                    .io_requirements()
                    .into_iter()
                    .map(|requirement| PortDescriptor {
                        name: requirement.name,
                        direction: requirement.direction,
                        kind: requirement.kind,
                        role: None,
                        point: None,
                    })
                    .collect(),
                parameters: ["gain", "limit"]
                    .into_iter()
                    .map(|name| ParameterDescriptor {
                        name: name.to_string(),
                        kind: ValueKind::Float,
                        range: Some(range),
                    })
                    .collect(),
                commands: Vec::new(),
                events: Vec::new(),
            }
        }

        fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
            let mismatch = || CommandError::ParameterTypeMismatch {
                component: self.name.to_string(),
                parameter: parameter.to_string(),
                expected: ValueKind::Float,
                found: value,
            };
            let mut gain = self.gain;
            let mut limit = self.limit;
            match parameter {
                "gain" => gain = f64::try_from(value).map_err(|_| mismatch())?,
                "limit" => limit = f64::try_from(value).map_err(|_| mismatch())?,
                _ => {
                    return Err(CommandError::UnknownParameter {
                        component: self.name.to_string(),
                        parameter: parameter.to_string(),
                    });
                }
            }
            if gain > limit {
                return Err(CommandError::InvalidParameter {
                    component: self.name.to_string(),
                    parameter: parameter.to_string(),
                    detail: "gain must not exceed limit".to_string(),
                });
            }
            self.gain = gain;
            self.limit = limit;
            Ok(())
        }

        fn report_parameters(&self) -> StateMap {
            let mut parameters = StateMap::new();
            parameters.insert("gain", Value::Float(self.gain));
            parameters.insert("limit", Value::Float(self.limit));
            parameters
        }

        fn capture_state(&self) -> StateMap {
            let mut state = StateMap::new();
            state.insert("gain", Value::Float(self.gain));
            state.insert("limit", Value::Float(self.limit));
            state
        }

        fn restore_state(&mut self, state: &StateMap) -> Result<(), dcs_core::StateError> {
            state.ensure_known_fields(self.name, &["gain", "limit"])?;
            self.gain = state.require_f64(self.name, "gain")?;
            self.limit = state.require_f64(self.name, "limit")?;
            Ok(())
        }
    }

    fn set_parameter(component: &str, name: &str, value: Value) -> Command {
        Command::SetParameter {
            component: component.to_string(),
            name: name.to_string(),
            value,
        }
    }

    /// Rig for parameter commands: `Tunable` ("loop") reads writable `In`
    /// point 10 and drives `Out` point 20 at gain 2, limit 10; `Scale`
    /// ("a") rides along as the parameterless component.
    fn tuning_rig(driver: &StubDriver) -> Executor<'_> {
        let map = PointMap::new()
            .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
            .with_point(PointId(20), Direction::Out, ValueKind::Float)
            .with_point(PointId(30), Direction::Out, ValueKind::Float);
        Executor::new(
            driver,
            map,
            vec![
                Box::new(Tunable {
                    name: "loop",
                    input: PointId(10),
                    output: PointId(20),
                    gain: 2.0,
                    limit: 10.0,
                }),
                Box::new(Scale {
                    name: "a",
                    input: PointId(10),
                    output: PointId(30),
                    gain: 1.0,
                }),
            ],
        )
        .unwrap()
    }

    #[test]
    fn parameter_command_applies_at_the_scan_boundary() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        driver.write(PointId(10), Value::Float(1.0)).unwrap();
        let mut executor = tuning_rig(&driver);
        executor.scan();
        assert_eq!(driver_value(&driver, 20), Value::Float(2.0));

        let command = set_parameter("loop", "gain", Value::Float(3.0));
        let receipt = executor.submit_command(command.clone());
        assert_eq!(
            receipt,
            CommandReceipt {
                command,
                outcome: CommandOutcome::Accepted {
                    apply_tick: Tick(2)
                },
                actor: None,
            }
        );
        // Queued, not yet applied: the component still runs the old gain.
        executor.scan();
        assert_eq!(
            executor.receipts().last().unwrap().outcome,
            CommandOutcome::Applied { tick: Tick(2) }
        );
        // The tuned gain changed the output deterministically: 1.0 * 3.
        assert_eq!(driver_value(&driver, 20), Value::Float(3.0));
    }

    #[test]
    fn parameter_command_rejections_name_the_offending_component() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        let mut executor = tuning_rig(&driver);

        // The component name resolves nothing registered.
        let receipt = executor.submit_command(set_parameter("nope", "gain", Value::Float(1.0)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::UnknownComponent {
                    component: "nope".to_string(),
                }
            }
        );
        if let CommandOutcome::Rejected { reason } = &receipt.outcome {
            assert_eq!(reason.component(), Some("nope"));
        }

        // A component whose kind declares no writable parameters.
        let receipt = executor.submit_command(set_parameter("a", "gain", Value::Float(1.0)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::UnsupportedParameter {
                    component: "a".to_string(),
                    parameter: "gain".to_string(),
                }
            }
        );

        // A parameter the descriptor does not declare.
        let receipt = executor.submit_command(set_parameter("loop", "bias", Value::Float(1.0)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::UnknownParameter {
                    component: "loop".to_string(),
                    parameter: "bias".to_string(),
                }
            }
        );

        // The value's kind differs from the declared kind.
        let receipt = executor.submit_command(set_parameter("loop", "gain", Value::Bool(true)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::ParameterTypeMismatch {
                    component: "loop".to_string(),
                    parameter: "gain".to_string(),
                    expected: ValueKind::Float,
                    found: Value::Bool(true),
                }
            }
        );

        // Every rejection carried the offending component and none were
        // queued: the next scan applies nothing.
        for receipt in executor.receipts() {
            if let CommandOutcome::Rejected { reason } = &receipt.outcome {
                assert!(reason.component().is_some(), "receipt={receipt:?}");
            }
        }
        executor.scan();
        assert_eq!(executor.receipts().len(), 4);
    }

    #[test]
    fn out_of_range_parameter_rejects_without_applying() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        driver.write(PointId(10), Value::Float(1.0)).unwrap();
        let mut executor = tuning_rig(&driver);

        // `gain` declares [0, 10]; 11 never queues.
        let receipt = executor.submit_command(set_parameter("loop", "gain", Value::Float(11.0)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::OutOfRange {
                    component: "loop".to_string(),
                    parameter: "gain".to_string(),
                    value: Value::Float(11.0),
                    range: ParameterRange {
                        min: Value::Float(0.0),
                        max: Value::Float(10.0),
                    },
                }
            }
        );
        executor.scan();
        // Untouched: the run still drives the constructor's gain.
        assert_eq!(driver_value(&driver, 20), Value::Float(2.0));
    }

    #[test]
    fn component_refusal_at_the_boundary_leaves_state_untouched() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        driver.write(PointId(10), Value::Float(1.0)).unwrap();
        let mut executor = tuning_rig(&driver);

        // limit=1.5 passes the declared range but the hook's
        // `gain <= limit` invariant — gain is still 2 — refuses it.
        let receipt = executor.submit_command(set_parameter("loop", "limit", Value::Float(1.5)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(1)
            }
        );
        executor.scan();
        assert_eq!(
            executor.receipts().last().unwrap().outcome,
            CommandOutcome::Rejected {
                reason: CommandError::InvalidParameter {
                    component: "loop".to_string(),
                    parameter: "limit".to_string(),
                    detail: "gain must not exceed limit".to_string(),
                }
            }
        );
        // A refused parameter changes nothing.
        executor.scan();
        assert_eq!(driver_value(&driver, 20), Value::Float(2.0));
    }

    #[test]
    fn parameter_commands_apply_in_submission_order_with_writes() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        driver.write(PointId(10), Value::Float(1.0)).unwrap();
        let mut executor = tuning_rig(&driver);

        // Three commands share one boundary: a parameter tune, a point
        // write, and a second tune that relies on the first having
        // landed — raising gain past the old limit is only legal
        // because limit was raised first.
        executor.submit_command(set_parameter("loop", "limit", Value::Float(8.0)));
        executor.submit_command(write_value(10, ValueKind::Float, Value::Float(2.0)));
        executor.submit_command(set_parameter("loop", "gain", Value::Float(4.0)));
        executor.scan();

        assert_eq!(
            executor
                .receipts()
                .iter()
                .map(|receipt| receipt.outcome.clone())
                .collect::<Vec<_>>(),
            vec![
                CommandOutcome::Applied { tick: Tick(1) },
                CommandOutcome::Applied { tick: Tick(1) },
                CommandOutcome::Applied { tick: Tick(1) },
            ]
        );
        // in=2 written at the boundary, gain=4 applied: out = 8 = limit.
        assert_eq!(driver_value(&driver, 20), Value::Float(8.0));
    }

    #[test]
    fn checkpoint_restores_tuned_parameters() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        driver.write(PointId(10), Value::Float(1.0)).unwrap();
        let mut executor = tuning_rig(&driver);
        executor.scan();
        executor.submit_command(set_parameter("loop", "gain", Value::Float(4.0)));
        executor.scan();
        assert_eq!(driver_value(&driver, 20), Value::Float(4.0));

        let checkpoint = executor.checkpoint();
        // The tuned gain is inside the component's captured state.
        assert_eq!(
            checkpoint.components["loop"].get("gain"),
            Some(Value::Float(4.0))
        );

        // A fresh executor — constructor tuning, not the command's —
        // restores to the tuned run state.
        let standby = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        standby.write(PointId(10), Value::Float(1.0)).unwrap();
        let mut restored = Executor::restore(
            &standby,
            [
                (PointId(10), Direction::In, ValueKind::Float),
                (PointId(20), Direction::Out, ValueKind::Float),
                (PointId(30), Direction::Out, ValueKind::Float),
            ]
            .into_iter()
            .collect(),
            vec![
                Box::new(Tunable {
                    name: "loop",
                    input: PointId(10),
                    output: PointId(20),
                    gain: 2.0,
                    limit: 10.0,
                }),
                Box::new(Scale {
                    name: "a",
                    input: PointId(10),
                    output: PointId(30),
                    gain: 1.0,
                }),
            ],
            &checkpoint,
            None,
        )
        .unwrap();
        restored.scan();
        assert_eq!(driver_value(&standby, 20), Value::Float(4.0));

        // A checkpoint/restore mid-tune reports identically from the
        // fresh executor: the restored run's parameter section matches
        // the captured one's exactly.
        assert_eq!(
            restored.snapshot().parameters,
            executor.snapshot().parameters
        );
    }

    #[test]
    fn snapshot_reports_current_parameter_values() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        driver.write(PointId(10), Value::Float(1.0)).unwrap();
        let mut executor = tuning_rig(&driver);
        executor.scan();

        // The section aligns 1:1 with the diagnostics and descriptors in
        // scan order and carries each declared parameter's standing
        // value.
        let snapshot = executor.snapshot();
        assert_eq!(snapshot.parameters.len(), snapshot.components.len());
        assert_eq!(snapshot.parameters.len(), snapshot.descriptors.len());
        let looped = &snapshot.parameters[0];
        assert_eq!(looped.name, "loop");
        assert_eq!(
            looped.values,
            [
                ("gain".to_string(), Value::Float(2.0)),
                ("limit".to_string(), Value::Float(10.0)),
            ]
            .into_iter()
            .collect()
        );

        // The reported values are what the same scan's checkpoint
        // persists for the component — one vocabulary.
        let captured = &executor.checkpoint().components["loop"];
        for (name, value) in &looped.values {
            assert_eq!(captured.get(name), Some(*value), "parameter {name}");
        }

        // A receipted tune reports its new value in the next snapshot.
        executor.submit_command(set_parameter("loop", "gain", Value::Float(4.0)));
        executor.scan();
        assert_eq!(
            executor.snapshot().parameters[0].values["gain"],
            Value::Float(4.0)
        );
    }

    #[test]
    fn parameterless_component_reports_an_empty_section() {
        let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
        driver.write(PointId(10), Value::Float(1.0)).unwrap();
        let mut executor = tuning_rig(&driver);
        executor.scan();

        // `Scale` declares no parameters and never overrides the hook:
        // its entry is present with an empty value set, and the rest of
        // its telemetry is unchanged.
        let scale = &executor.snapshot().parameters[1];
        assert_eq!(scale.name, "a");
        assert!(scale.values.is_empty());
    }

    /// A component whose `report_parameters` reports beyond the
    /// declared set — the snapshot must keep only the descriptor's
    /// declared names.
    struct OverReporting {
        name: &'static str,
        output: PointId,
    }

    impl Component for OverReporting {
        fn name(&self) -> &str {
            self.name
        }

        fn io_requirements(&self) -> Vec<IoRequirement> {
            vec![IoRequirement::output::<f64>("out", self.output)]
        }

        fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
            io.write_typed(self.output, 1.0)?;
            Ok(())
        }

        fn describe(&self) -> ComponentDescriptor {
            ComponentDescriptor {
                name: self.name.to_string(),
                kind: "over-reporting".to_string(),
                label: self.name.to_string(),
                ports: Vec::new(),
                parameters: vec![ParameterDescriptor {
                    name: "declared".to_string(),
                    kind: ValueKind::Float,
                    range: None,
                }],
                commands: Vec::new(),
                events: Vec::new(),
            }
        }

        fn report_parameters(&self) -> StateMap {
            let mut parameters = StateMap::new();
            parameters.insert("declared", Value::Float(1.0));
            // Internal state that is no declared parameter must not leak
            // into the snapshot's parameter section.
            parameters.insert("internal", Value::Float(2.0));
            parameters
        }
    }

    #[test]
    fn undeclared_reported_names_never_reach_the_snapshot() {
        let driver = StubDriver::new(&[float(20)], &[]);
        let mut executor = Executor::new(
            &driver,
            [(PointId(20), Direction::Out, ValueKind::Float)]
                .into_iter()
                .collect(),
            vec![Box::new(OverReporting {
                name: "over",
                output: PointId(20),
            })],
        )
        .unwrap();
        executor.scan();

        let reported = &executor.snapshot().parameters[0];
        assert_eq!(reported.name, "over");
        assert_eq!(
            reported.values,
            [("declared".to_string(), Value::Float(1.0))]
                .into_iter()
                .collect()
        );
    }

    #[test]
    fn parameter_sections_match_across_identical_runs() {
        let run = || {
            let driver = StubDriver::new(&[float(10), float(20), float(30)], &[]);
            driver.write(PointId(10), Value::Float(1.0)).unwrap();
            let mut executor = tuning_rig(&driver);
            executor.submit_command(set_parameter("loop", "gain", Value::Float(4.0)));
            executor.run(3);
            executor.snapshot().parameters
        };
        assert_eq!(run(), run());
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
        executor.run(3);

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
                    None,
                )
                .unwrap(),
                None => checkpoint_rig(&driver),
            };
            if checkpoint.is_none() {
                executor.run(3);
            }
            let mut samples = Vec::new();
            for _ in 0..2 {
                executor.scan();
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
        original.run(3);
        let checkpoint = original.checkpoint();

        // The standby driver observes the process itself — the same
        // input value must be supplied to it, as on live hardware.
        assert_eq!(run(Some(&checkpoint)), run(None));
    }

    #[test]
    fn restore_rejects_a_mismatched_component_set() {
        let driver = StubDriver::new(&[float(10), float(20), int(30)], &[]);
        let mut executor = checkpoint_rig(&driver);
        executor.scan();
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
            None,
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
        executor.scan();
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
            None,
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
        executor.scan();
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
            None,
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
                        point: Some(PointId(10)),
                    },
                    PortDescriptor {
                        name: "out".to_string(),
                        direction: Direction::Out,
                        kind: ValueKind::Float,
                        role: None,
                        point: Some(PointId(20)),
                    },
                ],
                parameters: Vec::new(),
                commands: Vec::new(),
                events: Vec::new(),
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
                            point: None,
                        },
                        PortDescriptor {
                            name: "out".to_string(),
                            direction: Direction::Out,
                            kind: ValueKind::Float,
                            role: Some(PortRole::Output),
                            point: None,
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
                    commands: Vec::new(),
                    events: Vec::new(),
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
        // The serving layer annotated each port with its bound point —
        // `Fancy`'s own `describe` reported none.
        assert_eq!(descriptor.ports[0].point, Some(PointId(10)));
        assert_eq!(descriptor.ports[1].point, Some(PointId(20)));
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
        run_a.scan();
        run_b.scan();

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

    /// A map with a held internal `In` point (a writable operator
    /// setpoint) and an internal `Out` point (a monitored component
    /// write).
    fn internal_map() -> PointMap {
        PointMap::new()
            .with_writable_internal(
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
        executor.scan();
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
        executor.scan();
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
        executor.scan();
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

        executor.scan();
        // Scan 1's input phase routed the `Out` point's seeded initial;
        // the producer's write lands on the `In` point at scan 2.
        assert_eq!(
            executor.sample(PointId(30)),
            Some(Sample::good(Value::Float(0.0), Tick(1)))
        );
        assert_eq!(driver_value(&driver, 40), Value::Float(0.0));

        executor.scan();
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
        executor.scan();

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
            None,
        )
        .unwrap();
        assert_eq!(
            restored.sample(PointId(10)),
            Some(Sample::good(Value::Float(7.0), Tick(1)))
        );
    }

    #[test]
    fn apply_converges_internal_in_samples() {
        // The standby half of the transfer: an already-running executor
        // realigns its held operator values to the checkpoint's, the same
        // convergence `restore` gives a fresh one.
        let driver = StubDriver::new(&[], &[]);
        let mut standby = internal_rig(&driver);

        let driver = StubDriver::new(&[], &[]);
        let mut active = internal_rig(&driver);
        active.submit_command(Command::WriteValue {
            point: PointId(10),
            kind: ValueKind::Float,
            value: Value::Float(7.0),
        });
        active.scan();
        let checkpoint = active.checkpoint();

        standby.apply(&checkpoint).unwrap();
        assert_eq!(
            standby.sample(PointId(10)),
            Some(Sample::good(Value::Float(7.0), Tick(1)))
        );

        // A checkpoint's internal entries must name image-carried `In`
        // points of the declared kind — a mismatched mapping is a named
        // rejection, not a silent write.
        let mut foreign = checkpoint.clone();
        foreign
            .internal
            .insert(PointId(20), Sample::good(Value::Float(1.0), Tick(1)));
        assert_eq!(
            standby.apply(&foreign).unwrap_err(),
            RestoreError::UnknownInternal { point: PointId(20) }
        );
        let mut foreign = checkpoint;
        foreign
            .internal
            .insert(PointId(10), Sample::good(Value::Int(7), Tick(1)));
        assert_eq!(
            standby.apply(&foreign).unwrap_err(),
            RestoreError::IncompatibleInternal {
                point: PointId(10),
                expected: ValueKind::Float,
                found: Value::Int(7),
            }
        );
        // A rejected apply changes nothing the run observes.
        assert_eq!(
            standby.sample(PointId(10)),
            Some(Sample::good(Value::Float(7.0), Tick(1)))
        );
    }

    /// The revised-model executor of the reinitialize tests: writable
    /// internal `In` point 10 (the carried operator value's landing), an
    /// internal `Out` point 20 the one component writes onto, and a
    /// writable internal `In` point 30 the old model never declared —
    /// the `initialized` leg of the report.
    fn revision_rig(driver: &StubDriver) -> Executor<'_> {
        Executor::new(
            driver,
            internal_map().with_writable_internal(
                PointId(30),
                Direction::In,
                ValueKind::Float,
                Value::Float(9.0),
            ),
            vec![Box::new(Scale {
                name: "a",
                input: PointId(10),
                output: PointId(20),
                gain: 2.0,
            })],
        )
        .unwrap()
        .with_model_fingerprint(ModelFingerprint::of(b"model-b"))
    }

    /// A checkpoint captured under `model-a` exercising every
    /// classification leg: a carried operator value and one the revision
    /// dropped, a carried and a dropped output, a removed component's
    /// state, a driver section, and a force on the carried point.
    fn foreign_checkpoint() -> Checkpoint {
        Checkpoint {
            format_version: CHECKPOINT_FORMAT_VERSION,
            model_fingerprint: Some(ModelFingerprint::of(b"model-a")),
            generation: None,
            tick: Tick(50),
            components: [
                ("a".to_string(), StateMap::new()),
                ("gone".to_string(), StateMap::new()),
            ]
            .into_iter()
            .collect(),
            driver: Some(StateMap::new()),
            outputs: [
                (PointId(20), Sample::good(Value::Float(4.5), Tick(49))),
                (PointId(21), Sample::good(Value::Float(8.0), Tick(49))),
            ]
            .into_iter()
            .collect(),
            internal: [
                (PointId(10), Sample::good(Value::Float(7.0), Tick(49))),
                (PointId(11), Sample::good(Value::Float(1.0), Tick(49))),
            ]
            .into_iter()
            .collect(),
            forces: [(PointId(10), Value::Float(3.0))].into_iter().collect(),
            receipts: Vec::new(),
            command_admission: CommandAdmissionCounts::default(),
            source_owns_field: None,
            line_proof: None,
        }
    }

    #[test]
    fn reinitialize_carries_the_documented_set_and_reports_the_rest() {
        let driver = StubDriver::new(&[], &[]);
        let mut executor = revision_rig(&driver);
        // The revision scanned before the crossing: its own image values
        // are evidence the apply leg overwrites exactly the carried set.
        executor.scan();

        let report = executor.reinitialize(&foreign_checkpoint()).unwrap();
        assert_eq!(report.from, Some(ModelFingerprint::of(b"model-a")));
        assert_eq!(report.to, Some(ModelFingerprint::of(b"model-b")));
        assert_eq!(report.resumed_at, Tick(50));
        assert_eq!(
            report.carried,
            vec![CarriedPoint {
                point: PointId(10),
                value: Value::Float(7.0),
            }]
        );
        assert_eq!(
            report.carried_outputs,
            vec![CarriedPoint {
                point: PointId(20),
                value: Value::Float(4.5),
            }]
        );
        assert_eq!(
            report.carried_forces,
            vec![ForcedPoint {
                point: PointId(10),
                value: Value::Float(3.0),
            }]
        );
        assert_eq!(
            report.dropped,
            vec![
                DroppedElement::InternalPoint { point: PointId(11) },
                DroppedElement::OutputPoint { point: PointId(21) },
                DroppedElement::Component {
                    name: "gone".to_string(),
                },
                DroppedElement::DriverState,
            ]
        );
        assert_eq!(report.reinitialized, vec!["a".to_string()]);
        assert_eq!(report.initialized, vec![PointId(30)]);

        // The carried samples land verbatim and the run resumes
        // numbering at the checkpoint's tick.
        assert_eq!(
            executor.sample(PointId(10)),
            Some(Sample::good(Value::Float(7.0), Tick(49)))
        );
        assert_eq!(
            executor.sample(PointId(20)),
            Some(Sample::good(Value::Float(4.5), Tick(49)))
        );
        assert_eq!(executor.tick(), Tick(50));

        executor.scan();
        // The carried force stands: the scan substitutes it onto the
        // point's image at Substituted quality, and the reinitialized
        // component computed on the forced value.
        assert_eq!(
            executor.sample(PointId(10)),
            Some(Sample::new(
                Value::Float(3.0),
                Quality::Uncertain(QualityReason::Substituted),
                Tick(51)
            ))
        );
        assert_eq!(
            executor.sample(PointId(20)),
            Some(Sample::good(Value::Float(6.0), Tick(51)))
        );
        // The new point stands at its declared initial — initialized,
        // never carried.
        assert_eq!(
            executor.sample(PointId(30)),
            Some(Sample::good(Value::Float(9.0), Tick(0)))
        );
    }

    #[test]
    fn reinitialize_rejects_a_retyped_carried_point_before_applying() {
        let driver = StubDriver::new(&[], &[]);
        let mut executor = revision_rig(&driver);

        // The carried operator value's kind disagrees with the revision's
        // declaration under the same identity — a retype must rename, not
        // reinterpret.
        let mut checkpoint = foreign_checkpoint();
        checkpoint
            .internal
            .insert(PointId(10), Sample::good(Value::Int(7), Tick(49)));
        assert_eq!(
            executor.reinitialize(&checkpoint).unwrap_err(),
            CarryoverError::InternalKindMismatch {
                point: PointId(10),
                expected: ValueKind::Float,
                found: Value::Int(7),
            }
        );

        // A forced point the revision cannot serve — here the writable
        // `In` mark is gone — fails the crossing: an active force is
        // never released silently.
        let mut checkpoint = foreign_checkpoint();
        checkpoint.forces.insert(PointId(31), Value::Float(1.0));
        assert_eq!(
            executor.reinitialize(&checkpoint).unwrap_err(),
            CarryoverError::ForceNotServed { point: PointId(31) }
        );

        // Nothing applied: the run keeps its last-defined state and tick.
        assert_eq!(executor.tick(), Tick(0));
        assert_eq!(
            executor.sample(PointId(10)),
            Some(Sample::good(Value::Float(2.5), Tick(0)))
        );
        assert!(executor.snapshot().forces.is_empty());
    }

    /// The reinitialize rig with a tunable component: `Tunable` ("loop")
    /// declares `gain`/`limit` at the construction defaults 2.0/10.0 —
    /// the revision's declared defaults the itemization diffs against.
    fn revision_tuning_rig(driver: &StubDriver) -> Executor<'_> {
        Executor::new(
            driver,
            internal_map(),
            vec![Box::new(Tunable {
                name: "loop",
                input: PointId(10),
                output: PointId(20),
                gain: 2.0,
                limit: 10.0,
            })],
        )
        .unwrap()
        .with_model_fingerprint(ModelFingerprint::of(b"model-b"))
    }

    /// A checkpoint captured under `model-a` carrying `loop`'s
    /// component state — the fields `Tunable::capture_state` writes.
    fn tuned_checkpoint(state: StateMap) -> Checkpoint {
        Checkpoint {
            format_version: CHECKPOINT_FORMAT_VERSION,
            model_fingerprint: Some(ModelFingerprint::of(b"model-a")),
            generation: None,
            tick: Tick(50),
            components: [("loop".to_string(), state)].into_iter().collect(),
            driver: None,
            outputs: BTreeMap::new(),
            internal: BTreeMap::new(),
            forces: BTreeMap::new(),
            receipts: Vec::new(),
            command_admission: CommandAdmissionCounts::default(),
            source_owns_field: None,
            line_proof: None,
        }
    }

    #[test]
    fn reinitialize_itemizes_reverted_tuning() {
        let driver = StubDriver::new(&[], &[]);
        let mut executor = revision_tuning_rig(&driver);

        // The receipted tune the old run settled: `gain` driven to 3.0
        // while `limit` stayed at its declared 10.0 — the checkpoint's
        // component section carries both, exactly as `capture_state`
        // wrote them.
        let mut state = StateMap::new();
        state.insert("gain", Value::Float(3.0));
        state.insert("limit", Value::Float(10.0));
        let checkpoint = tuned_checkpoint(state);

        let report = executor.reinitialize(&checkpoint).unwrap();
        assert_eq!(report.reinitialized, vec!["loop".to_string()]);
        // Only the differing tune is named: `limit` matched the
        // revision's declared default and lists nothing.
        assert_eq!(
            report.reverted_tuning,
            vec![RevertedParameter {
                component: "loop".to_string(),
                parameter: "gain".to_string(),
                checkpointed: Value::Float(3.0),
                declared: Value::Float(2.0),
            }]
        );

        // The tune does not carry: the reinitialized component stands
        // at its declared default — the itemization is the witnessed
        // record of what reverted.
        let parameters = &executor.snapshot().parameters[0];
        assert_eq!(parameters.name, "loop");
        assert_eq!(parameters.values["gain"], Value::Float(2.0));

        // Two runs of the same crossing report identically.
        let mut second = revision_tuning_rig(&driver);
        assert_eq!(second.reinitialize(&checkpoint).unwrap(), report);
    }

    #[test]
    fn reinitialize_itemizes_against_declared_defaults_not_live_tuning() {
        let driver = StubDriver::new(&[], &[]);
        let mut executor = revision_tuning_rig(&driver);

        // This run's own `loop` got the same 3.0 tune — the shape an
        // adopted `Accepted` receipt re-settling under the revision
        // produces. The itemization diffs the checkpoint against the
        // revision's declared defaults, not the live report, so the
        // re-settled tune cannot hide what the old run's value
        // reverted from.
        executor.submit_command(set_parameter("loop", "gain", Value::Float(3.0)));
        executor.scan();
        let parameters = &executor.snapshot().parameters[0];
        assert_eq!(parameters.values["gain"], Value::Float(3.0));

        let mut state = StateMap::new();
        state.insert("gain", Value::Float(3.0));
        state.insert("limit", Value::Float(10.0));
        let report = executor.reinitialize(&tuned_checkpoint(state)).unwrap();
        assert_eq!(
            report.reverted_tuning,
            vec![RevertedParameter {
                component: "loop".to_string(),
                parameter: "gain".to_string(),
                checkpointed: Value::Float(3.0),
                declared: Value::Float(2.0),
            }]
        );
    }

    #[test]
    fn reinitialize_itemizes_a_retyped_parameter_and_skips_undeclared_state() {
        let driver = StubDriver::new(&[], &[]);
        let mut executor = revision_tuning_rig(&driver);

        // `gain` checkpointed as Int where the revision declares Float:
        // nothing component-side crosses, so the field itemizes as
        // reverted like any differing value — the named-refusal
        // convention binds the carried sections, not state that never
        // moves. `integrator` is captured run state the descriptor does
        // not declare: the component's own dropped vocabulary, not a
        // reverted parameter.
        let mut state = StateMap::new();
        state.insert("gain", Value::Int(3));
        state.insert("integrator", Value::Float(1.5));
        let report = executor.reinitialize(&tuned_checkpoint(state)).unwrap();
        assert_eq!(
            report.reverted_tuning,
            vec![RevertedParameter {
                component: "loop".to_string(),
                parameter: "gain".to_string(),
                checkpointed: Value::Int(3),
                declared: Value::Float(2.0),
            }]
        );
    }

    #[test]
    fn reinitialize_ignores_the_fingerprint_gate_but_not_the_version() {
        let driver = StubDriver::new(&[], &[]);
        let mut executor = revision_rig(&driver);

        // The fingerprint mismatch that `apply` refuses is the expected
        // case here — it is the revision marker, not an error.
        let checkpoint = foreign_checkpoint();
        assert!(executor.apply(&checkpoint).is_err());
        assert!(executor.reinitialize(&checkpoint).is_ok());

        // But the format negotiation still applies: an unreadable
        // version fails before any state moves.
        let mut checkpoint = foreign_checkpoint();
        checkpoint.format_version = 99;
        assert_eq!(
            executor.reinitialize(&checkpoint).unwrap_err(),
            CarryoverError::UnsupportedVersion {
                found: 99,
                supported: SUPPORTED_FORMAT_VERSIONS,
            }
        );
    }

    #[test]
    fn checkpoint_carries_version_and_fingerprint_through_serde() {
        let driver = StubDriver::new(&[float(10), float(20), int(30)], &[]);
        let mut executor =
            checkpoint_rig(&driver).with_model_fingerprint(ModelFingerprint::of(b"model-a"));
        executor.scan();
        let checkpoint = executor.checkpoint();

        // This build stamps the version it writes and the fingerprint
        // the assembling layer supplied.
        assert_eq!(checkpoint.format_version, CHECKPOINT_FORMAT_VERSION);
        assert_eq!(
            checkpoint.model_fingerprint,
            Some(ModelFingerprint::of(b"model-a"))
        );

        // Both negotiate on the wire: the serialized form carries the
        // fields and they survive a round-trip.
        let json = serde_json::to_string(&checkpoint).unwrap();
        let document: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(document["format_version"], CHECKPOINT_FORMAT_VERSION);
        assert_eq!(
            document["model_fingerprint"],
            ModelFingerprint::of(b"model-a").0
        );
        assert_eq!(
            serde_json::from_str::<Checkpoint>(&json).unwrap(),
            checkpoint
        );
    }

    #[test]
    fn legacy_checkpoint_without_version_restores() {
        // The documented compatible-version case: a checkpoint a
        // pre-versioning build wrote carries no `format_version` —
        // serde reads it as version 0, which
        // `SUPPORTED_FORMAT_VERSIONS` accepts — and no fingerprint, so
        // it restores onto an executor assembled without one.
        let driver = StubDriver::new(&[float(10), float(20), int(30)], &[]);
        let mut executor = checkpoint_rig(&driver);
        executor.run(3);
        let checkpoint = executor.checkpoint();
        assert_eq!(checkpoint.model_fingerprint, None);

        let mut document = serde_json::to_value(&checkpoint).unwrap();
        document.as_object_mut().unwrap().remove("format_version");
        assert!(
            !document
                .as_object()
                .unwrap()
                .contains_key("model_fingerprint")
        );
        let legacy: Checkpoint = serde_json::from_value(document).unwrap();
        assert_eq!(legacy.format_version, 0);
        assert_eq!(legacy.model_fingerprint, None);

        let standby = StubDriver::new(&[float(10), float(20), int(30)], &[]);
        let restored = Executor::restore(
            &standby,
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
            &legacy,
            None,
        )
        .unwrap();
        assert_eq!(restored.tick(), Tick(3));

        // And a running unfingerprinted standby applies it in place.
        let tracking_driver = StubDriver::new(&[float(10), float(20), int(30)], &[]);
        let mut tracking = checkpoint_rig(&tracking_driver);
        tracking.apply(&legacy).unwrap();
        assert_eq!(tracking.tick(), Tick(3));
    }

    #[test]
    fn restore_rejects_an_unsupported_format_version() {
        let driver = StubDriver::new(&[float(10), float(20), int(30)], &[]);
        let mut executor = checkpoint_rig(&driver);
        executor.scan();
        let mut checkpoint = executor.checkpoint();
        checkpoint.format_version = CHECKPOINT_FORMAT_VERSION + 1;

        let error = Executor::restore(
            &StubDriver::new(&[float(10), float(20), int(30)], &[]),
            [
                (PointId(10), Direction::In, ValueKind::Float),
                (PointId(20), Direction::Out, ValueKind::Float),
                (PointId(30), Direction::Out, ValueKind::Int),
            ]
            .into_iter()
            .collect(),
            vec![Box::new(Counter {
                name: "count",
                output: PointId(30),
                count: 0,
            })],
            &checkpoint,
            None,
        )
        .unwrap_err();
        assert_eq!(
            error,
            RestoreError::UnsupportedVersion {
                found: CHECKPOINT_FORMAT_VERSION + 1,
                supported: SUPPORTED_FORMAT_VERSIONS,
            }
        );

        // The same rejection precedes any state on a running standby:
        // the version is negotiated before the component-set check, so
        // this checkpoint's components would have failed it anyway.
        let standby_driver = StubDriver::new(&[float(10), float(20), int(30)], &[]);
        let mut standby = checkpoint_rig(&standby_driver);
        standby.run(2);
        assert_eq!(standby.apply(&checkpoint), Err(error));
        assert_eq!(standby.tick(), Tick(2));
        assert_eq!(
            standby.sample(PointId(30)),
            Some(Sample::good(Value::Int(2), Tick(2)))
        );
    }

    #[test]
    fn restore_rejects_a_mismatched_model_fingerprint() {
        let fingerprint = ModelFingerprint::of(b"model-a");
        let other = ModelFingerprint::of(b"model-b");

        let driver = StubDriver::new(&[float(10), float(20), int(30)], &[]);
        driver.write(PointId(10), Value::Float(5.0)).unwrap();
        let mut executor = checkpoint_rig(&driver).with_model_fingerprint(fingerprint);
        executor.run(3);
        let checkpoint = executor.checkpoint();

        let components = || {
            vec![
                Box::new(Scale {
                    name: "a",
                    input: PointId(10),
                    output: PointId(20),
                    gain: 2.0,
                }) as Box<dyn Component>,
                Box::new(Counter {
                    name: "count",
                    output: PointId(30),
                    count: 0,
                }),
            ]
        };
        let map = || {
            [
                (PointId(10), Direction::In, ValueKind::Float),
                (PointId(20), Direction::Out, ValueKind::Float),
                (PointId(30), Direction::Out, ValueKind::Int),
            ]
            .into_iter()
            .collect::<PointMap>()
        };

        // A checkpoint from a different model — same component set,
        // different fingerprint — is rejected by fingerprint before any
        // state applies.
        let error = Executor::restore(
            &StubDriver::new(&[float(10), float(20), int(30)], &[]),
            map(),
            components(),
            &checkpoint,
            Some(other),
        )
        .unwrap_err();
        assert_eq!(
            error,
            RestoreError::FingerprintMismatch {
                found: Some(fingerprint),
                expected: Some(other),
            }
        );
        // So is a fingerprinted checkpoint landing on a run assembled
        // without one — the model identity cannot be verified.
        let error = Executor::restore(
            &StubDriver::new(&[float(10), float(20), int(30)], &[]),
            map(),
            components(),
            &checkpoint,
            None,
        )
        .unwrap_err();
        assert_eq!(
            error,
            RestoreError::FingerprintMismatch {
                found: Some(fingerprint),
                expected: None,
            }
        );

        // A running standby fingerprinted for a different model rejects
        // the same checkpoint and keeps its last-good alignment: nothing
        // of the foreign run applied.
        let standby_driver = StubDriver::new(&[float(10), float(20), int(30)], &[]);
        let mut standby = checkpoint_rig(&standby_driver).with_model_fingerprint(other);
        standby.run(2);
        assert_eq!(
            standby.apply(&checkpoint),
            Err(RestoreError::FingerprintMismatch {
                found: Some(fingerprint),
                expected: Some(other),
            })
        );
        assert_eq!(standby.tick(), Tick(2));
        assert_eq!(
            standby.sample(PointId(30)),
            Some(Sample::good(Value::Int(2), Tick(2)))
        );

        // The matching pair restores and keeps emitting the fingerprint.
        let restored_driver = StubDriver::new(&[float(10), float(20), int(30)], &[]);
        let restored = Executor::restore(
            &restored_driver,
            map(),
            components(),
            &checkpoint,
            Some(fingerprint),
        )
        .unwrap();
        assert_eq!(restored.tick(), Tick(3));
        assert_eq!(restored.checkpoint().model_fingerprint, Some(fingerprint));
    }

    // ── The cyclic field-I/O exchange contract ──────────────────────────

    /// What the stub's next `exchange` does — the scripted transport.
    #[derive(Clone, Copy)]
    enum Exchange {
        /// A clean exchange: the staged output image publishes and the
        /// input image latches atomically at the exchange's tick.
        Complete,
        /// The frame returned complete but past its deadline: the
        /// exchange succeeds and counts a missed deadline.
        Late,
        /// The exchange completed short — a working-counter shortfall
        /// naming the station: its points escalate to `Disconnected`
        /// until a clean exchange; the rest of the image latches.
        Short(u64),
        /// The exchange did not complete: nothing publishes or latches,
        /// the staged output image is retained, and the miss counts.
        Failed,
    }

    /// A scripted [`CyclicIoDriver`] stub proving the cyclic exchange
    /// contract. `exchange` is the only call allowed to touch the
    /// simulated field — `read` serves the input image the last
    /// completed exchange latched and `write` stages the pending output
    /// image — and the instrumentation counters prove per-point access
    /// never transports.
    struct CyclicStub {
        /// Every point the process image covers, by station.
        points: HashMap<PointId, CyclicPoint>,
        state: Mutex<CyclicState>,
    }

    /// A point's bus-side identity: its declared value kind and the
    /// station the image attributes it to.
    #[derive(Clone, Copy)]
    struct CyclicPoint {
        kind: ValueKind,
        station: u64,
    }

    /// The stub's mutable bus and instrumentation state.
    struct CyclicState {
        /// The simulated field — the only data `exchange` may move.
        field: HashMap<PointId, Sample>,
        /// The held input image: the field snapshot the last completed
        /// exchange latched, stamped with the producing exchange's tick.
        latched: HashMap<PointId, Sample>,
        /// The pending output image `write` stages — retained across a
        /// failed exchange, published by the next completed one.
        staged: HashMap<PointId, Value>,
        /// The scripted exchange outcomes, consumed in order; an
        /// exhausted script completes cleanly.
        script: VecDeque<Exchange>,
        /// Consecutive uncompleted exchanges — the miss count the
        /// declared `exchange_miss_threshold` compares against.
        misses: u64,
        /// The stub's `exchange_miss_threshold` device parameter: reads
        /// escalate to [`IoError::Disconnected`] once `misses` reaches
        /// it.
        miss_threshold: u64,
        /// The stations a working-counter shortfall last named — their
        /// points escalate to `Disconnected` until a clean exchange.
        short_stations: HashSet<u64>,
        /// The exchange counters `diagnostics` reports.
        attempted: u64,
        completed: u64,
        shortfalls: u64,
        missed_deadlines: u64,
        last_exchange_tick: Option<Tick>,
        last_error: Option<String>,
        /// Calls into the simulated transport — `exchange` is the only
        /// one permitted; per-point `read`/`write` must never add one.
        transport_calls: u64,
        /// Per-point access counts — evidence the reads and writes ran.
        reads: u64,
        writes: u64,
        /// The boundary call log — `exchange@tick`, `read@point`,
        /// `write@point` — ordering evidence for the contract tests.
        calls: Vec<String>,
    }

    impl CyclicStub {
        /// A stub over `(point id, station id)` pairs — every point
        /// `Float`, field-seeded at `0.0` — with the declared
        /// `exchange_miss_threshold` and the scripted exchange outcomes.
        fn new(points: &[(u64, u64)], miss_threshold: u64, script: &[Exchange]) -> Self {
            assert!(
                miss_threshold >= 1,
                "a miss threshold under 1 escalates every read"
            );
            let points: HashMap<PointId, CyclicPoint> = points
                .iter()
                .map(|&(point, station)| {
                    (
                        PointId(point),
                        CyclicPoint {
                            kind: ValueKind::Float,
                            station,
                        },
                    )
                })
                .collect();
            // The input image is seeded with the field's initial
            // contents — as a pre-run exchange would leave it — so a
            // declared point has a defined held sample at Tick::ZERO.
            let field: HashMap<PointId, Sample> = points
                .keys()
                .map(|&point| (point, Sample::good(Value::Float(0.0), Tick::ZERO)))
                .collect();
            Self {
                points,
                state: Mutex::new(CyclicState {
                    latched: field.clone(),
                    field,
                    staged: HashMap::new(),
                    script: script.iter().copied().collect(),
                    misses: 0,
                    miss_threshold,
                    short_stations: HashSet::new(),
                    attempted: 0,
                    completed: 0,
                    shortfalls: 0,
                    missed_deadlines: 0,
                    last_exchange_tick: None,
                    last_error: None,
                    transport_calls: 0,
                    reads: 0,
                    writes: 0,
                    calls: Vec::new(),
                }),
            }
        }

        /// Plants `value` on the field alone — what a device asserts
        /// between exchanges. The held input image only sees it once an
        /// exchange latches it.
        fn field_seed(&self, point: u64, value: f64) {
            self.state.lock().unwrap().field.insert(
                PointId(point),
                Sample::good(Value::Float(value), Tick::ZERO),
            );
        }

        /// The sample the simulated field currently carries for `point`
        /// — the published output side, for test observation only.
        fn field_sample(&self, point: u64) -> Option<Sample> {
            self.state
                .lock()
                .unwrap()
                .field
                .get(&PointId(point))
                .copied()
        }

        /// The transport-call count — `exchange` is the only increment.
        fn transport_calls(&self) -> u64 {
            self.state.lock().unwrap().transport_calls
        }

        /// The per-point `(reads, writes)` counts.
        fn point_accesses(&self) -> (u64, u64) {
            let state = self.state.lock().unwrap();
            (state.reads, state.writes)
        }

        /// The boundary call log.
        fn calls(&self) -> Vec<String> {
            self.state.lock().unwrap().calls.clone()
        }

        /// The exchange counters `diagnostics` reports.
        fn exchange_counters(&self) -> ExchangeDiagnostics {
            self.diagnostics().unwrap().exchange.unwrap()
        }
    }

    impl IoDriver for CyclicStub {
        /// Serves the held input image — never the transport. Once the
        /// miss count reaches the declared `exchange_miss_threshold`, or
        /// a working-counter shortfall named the point's station, the
        /// read escalates to `Disconnected`.
        fn read(&self, point: PointId) -> Result<Sample, IoError> {
            let mut state = self.state.lock().unwrap();
            state.reads += 1;
            state.calls.push(format!("read@{}", point.0));
            let spec = *self
                .points
                .get(&point)
                .ok_or(IoError::UnknownPoint(point))?;
            if state.misses >= state.miss_threshold || state.short_stations.contains(&spec.station)
            {
                return Err(IoError::Disconnected(point));
            }
            state
                .latched
                .get(&point)
                .copied()
                .ok_or(IoError::UnknownPoint(point))
        }

        /// Stages the pending output image — never the transport.
        /// `UnknownPoint`/`TypeMismatch` semantics are the same as any
        /// point-wise driver's.
        fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
            let mut state = self.state.lock().unwrap();
            state.writes += 1;
            state.calls.push(format!("write@{}", point.0));
            let spec = *self
                .points
                .get(&point)
                .ok_or(IoError::UnknownPoint(point))?;
            if value.kind() != spec.kind {
                return Err(IoError::TypeMismatch {
                    point,
                    expected: spec.kind,
                    found: value,
                });
            }
            state.staged.insert(point, value);
            Ok(())
        }

        /// The exchange counters and the link state a cyclic driver
        /// reports: `Disconnected` while any miss stands, with the last
        /// failure's description.
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
                    succeeded: state.completed,
                    working_counter_mismatches: state.shortfalls,
                    last_exchange_tick: state.last_exchange_tick,
                    missed_deadlines: state.missed_deadlines,
                }),
            })
        }

        /// The stub implements the cyclic contract.
        fn cyclic(&self) -> Option<&(dyn CyclicIoDriver + Sync)> {
            Some(self)
        }
    }

    impl CyclicIoDriver for CyclicStub {
        /// One process-image exchange for `tick`: the staged output
        /// image publishes, then the returned input image latches
        /// atomically at `tick` — the acquisition stamp a
        /// `stale_after_ticks` budget measures. The scripted outcome
        /// decides whether anything moves at all.
        fn exchange(&self, tick: Tick) -> Result<(), IoError> {
            let mut guard = self.state.lock().unwrap();
            let state = &mut *guard;
            state.transport_calls += 1;
            state.attempted += 1;
            state.calls.push(format!("exchange@{}", tick.0));
            match state.script.pop_front().unwrap_or(Exchange::Complete) {
                Exchange::Failed => {
                    // Nothing publishes or latches — the held input
                    // image serves the reads that follow and the staged
                    // output image is retained for the next exchange.
                    state.misses += 1;
                    state.last_error = Some("the exchange did not complete".to_string());
                    Err(IoError::Disconnected(
                        *self.points.keys().min().expect("nonempty image"),
                    ))
                }
                outcome => {
                    // Publish the staged output image, then latch the
                    // answering stations' data into the input image at
                    // the acquisition stamp — atomically, so a scan
                    // never reads a half-moved image.
                    let short = match outcome {
                        Exchange::Short(station) => Some(station),
                        _ => None,
                    };
                    for (&point, &value) in &state.staged {
                        state.field.insert(point, Sample::good(value, tick));
                    }
                    state.staged.clear();
                    for (&point, &sample) in &state.field {
                        if short.is_none_or(|station| self.points[&point].station != station) {
                            state.latched.insert(point, Sample { tick, ..sample });
                        }
                    }
                    state.short_stations = short.into_iter().collect();
                    state.misses = 0;
                    state.completed += 1;
                    state.last_exchange_tick = Some(tick);
                    match outcome {
                        Exchange::Late => state.missed_deadlines += 1,
                        Exchange::Short(station) => {
                            state.shortfalls += 1;
                            state.last_error = Some(format!(
                                "station {station} answered short of its working counter"
                            ));
                        }
                        _ => {}
                    }
                    Ok(())
                }
            }
        }
    }

    /// Writes `value` to `output` on its first step only — so a later
    /// exchange publishing it proves the staged image survived, not a
    /// restage.
    struct WriteOnce {
        output: PointId,
        value: f64,
        done: bool,
    }

    impl Component for WriteOnce {
        fn name(&self) -> &str {
            "once"
        }

        fn io_requirements(&self) -> Vec<IoRequirement> {
            vec![IoRequirement::output::<f64>("out", self.output)]
        }

        fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
            if !self.done {
                self.done = true;
                io.write_typed(self.output, self.value)?;
            }
            Ok(())
        }
    }

    #[test]
    fn cyclic_exchange_runs_once_per_scan_at_the_read_boundary() {
        let driver = CyclicStub::new(&[(10, 1), (11, 1), (20, 1)], 3, &[]);
        let map = PointMap::new()
            .with_point(PointId(10), Direction::In, ValueKind::Float)
            .with_writable_point(PointId(11), Direction::In, ValueKind::Float)
            .with_point(PointId(20), Direction::Out, ValueKind::Float);
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

        // A command submitted between scans applies at the next scan's
        // boundary — its driver write stages into the output image
        // before the exchange runs.
        executor.submit_command(write_value(11, ValueKind::Float, Value::Float(3.0)));
        executor.scan();
        executor.scan();

        // Exactly one exchange per scan, stamped with the scan's tick —
        // after the boundary's command write stages, before the
        // per-point reads serve the latched image, before the write
        // phase stages the scan's outputs.
        assert_eq!(
            driver.calls(),
            vec![
                "write@11",   // the queued command applies …
                "exchange@1", // … then the exchange turns the image …
                "read@10",
                "read@11",  // … then the input phase reads it …
                "write@20", // … and the write phase stages the output
                "exchange@2",
                "read@10",
                "read@11",
                "write@20",
            ]
        );
    }

    #[test]
    fn cyclic_point_access_never_touches_the_transport() {
        let driver = CyclicStub::new(&[(10, 1), (20, 1)], 3, &[]);
        let map = PointMap::new()
            .with_point(PointId(10), Direction::In, ValueKind::Float)
            .with_point(PointId(20), Direction::Out, ValueKind::Float);
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

        executor.run(3);

        // Three scans read the input and wrote the output every cycle —
        // yet the only transport calls are the three exchanges: under
        // the cyclic contract `read` serves the latched input image and
        // `write` stages the pending output image, neither touching the
        // bus.
        assert_eq!(driver.point_accesses(), (3, 3));
        assert_eq!(driver.transport_calls(), 3);
    }

    #[test]
    fn cyclic_held_image_ages_to_stale_then_escalates_past_threshold() {
        // Miss threshold 3, stale budget 1: a held input keeps serving
        // while misses accumulate, aging under its budget, until the
        // third miss escalates its reads to `Disconnected`.
        let driver = CyclicStub::new(
            &[(10, 1)],
            3,
            &[
                Exchange::Complete,
                Exchange::Failed,
                Exchange::Failed,
                Exchange::Failed,
                Exchange::Failed,
                Exchange::Complete,
            ],
        );
        driver.field_seed(10, 7.0);
        let mut executor = Executor::new(
            &driver,
            stale_map(PointId(10), 1),
            vec![Box::new(Declared {
                name: "idle",
                requirements: vec![IoRequirement::input::<f64>("in", PointId(10))],
            })],
        )
        .unwrap();

        // The first exchange latches the field image at its tick.
        executor.scan();
        assert_eq!(
            executor.snapshot().points[0].sample.unwrap(),
            Sample::good(Value::Float(7.0), Tick(1))
        );

        // The field moved on — the held image cannot see it until an
        // exchange latches again.
        driver.field_seed(10, 9.0);

        // The first miss: the exchange failed — counted once at the
        // boundary and surfaced as a disconnected link — but the held
        // image still serves; the acquisition stamp lags by one, inside
        // the budget.
        executor.scan();
        let health = &executor.snapshot().io_health;
        assert_eq!(health.failed_exchanges, 1);
        assert_eq!(health.failed_reads, 0);
        assert_eq!(
            health.last_error,
            Some(IoFault {
                tick: Tick(2),
                point: PointId(10),
                direction: Direction::In,
                error: IoError::Disconnected(PointId(10)),
            })
        );
        assert_eq!(
            health.driver.as_ref().unwrap().link,
            LinkState::Disconnected
        );
        assert_eq!(
            executor.snapshot().points[0].sample.unwrap(),
            Sample::good(Value::Float(7.0), Tick(2))
        );

        // The second miss ages the held sample past its budget — the
        // latched acquisition stamp still reads tick 1.
        executor.scan();
        let sample = executor.snapshot().points[0].sample.unwrap();
        assert_eq!(sample.value, Value::Float(7.0));
        assert_eq!(sample.quality, Quality::Uncertain(QualityReason::Stale));

        // The third miss reaches the declared threshold: reads escalate
        // to `Disconnected` — an ordinary boundary failure degrading the
        // held value to `Bad`.
        executor.scan();
        let sample = executor.snapshot().points[0].sample.unwrap();
        assert_eq!(sample.value, Value::Float(7.0));
        assert_eq!(
            sample.quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
        let health = executor.snapshot().io_health;
        assert_eq!(health.failed_exchanges, 3);
        assert_eq!(health.failed_reads, 1);
        assert_eq!(
            health.driver.unwrap(),
            DriverDiagnostics {
                link: LinkState::Disconnected,
                last_error: Some("the exchange did not complete".to_string()),
                exchange: Some(ExchangeDiagnostics {
                    attempted: 4,
                    succeeded: 1,
                    working_counter_mismatches: 0,
                    last_exchange_tick: Some(Tick(1)),
                    missed_deadlines: 0,
                }),
            }
        );

        // The fourth miss keeps the escalation; the sixth scan's
        // completed exchange relatches — misses reset, the link
        // recovers, and the field's asserted value lands fresh.
        executor.scan();
        assert_eq!(
            executor.snapshot().points[0].sample.unwrap().quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
        executor.scan();
        assert_eq!(
            executor.snapshot().points[0].sample.unwrap(),
            Sample::good(Value::Float(9.0), Tick(6))
        );
        assert_eq!(
            executor.snapshot().io_health.driver.unwrap().link,
            LinkState::Connected
        );
    }

    #[test]
    fn cyclic_output_image_is_retained_across_a_failed_exchange() {
        let driver = CyclicStub::new(
            &[(20, 1)],
            3,
            &[Exchange::Complete, Exchange::Failed, Exchange::Complete],
        );
        let map: PointMap = [(PointId(20), Direction::Out, ValueKind::Float)]
            .into_iter()
            .collect();
        let mut executor = Executor::new(
            &driver,
            map,
            vec![Box::new(WriteOnce {
                output: PointId(20),
                value: 9.0,
                done: false,
            })],
        )
        .unwrap();

        // Scan 1's write phase staged the value — the field carries the
        // initial sample until the next exchange publishes it.
        executor.scan();
        assert_eq!(
            driver.field_sample(20),
            Some(Sample::good(Value::Float(0.0), Tick::ZERO))
        );

        // Scan 2's exchange fails: nothing publishes, and the staged
        // image is retained — no component write follows to restage it.
        executor.scan();
        assert_eq!(
            driver.field_sample(20),
            Some(Sample::good(Value::Float(0.0), Tick::ZERO))
        );

        // Scan 3's completed exchange publishes the retained staged
        // image — the value lands stamped with the exchange's tick.
        executor.scan();
        assert_eq!(
            driver.field_sample(20),
            Some(Sample::good(Value::Float(9.0), Tick(3)))
        );
    }

    #[test]
    fn cyclic_actuation_delay_is_one_scan() {
        // The documented delay: a value a component writes in scan `t`
        // stages into the output image and publishes in scan `t + 1`'s
        // exchange — never inside the scan that wrote it.
        let driver = CyclicStub::new(&[(20, 1)], 3, &[]);
        let map: PointMap = [(PointId(20), Direction::Out, ValueKind::Float)]
            .into_iter()
            .collect();
        let mut executor = Executor::new(
            &driver,
            map,
            vec![Box::new(Constant {
                name: "const",
                output: PointId(20),
                value: 9.0,
            })],
        )
        .unwrap();

        // Scan 1: the exchange ran before the write phase staged
        // anything, so the field still carries its initial value while
        // the executor's image already reports the staged output.
        executor.scan();
        assert_eq!(
            driver.field_sample(20),
            Some(Sample::good(Value::Float(0.0), Tick::ZERO))
        );
        assert_eq!(
            executor.sample(PointId(20)),
            Some(Sample::good(Value::Float(9.0), Tick(1)))
        );

        // Scan 2's exchange publishes the staged image — one scan after
        // the write.
        executor.scan();
        assert_eq!(
            driver.field_sample(20),
            Some(Sample::good(Value::Float(9.0), Tick(2)))
        );
    }

    #[test]
    fn cyclic_short_exchange_degrades_only_the_named_station() {
        // Station 1 serves point 10, station 2 point 11: a
        // working-counter shortfall naming station 1 escalates its
        // points alone while the rest of the image latched fresh.
        let driver = CyclicStub::new(
            &[(10, 1), (11, 2)],
            3,
            &[Exchange::Complete, Exchange::Short(1), Exchange::Complete],
        );
        driver.field_seed(10, 7.0);
        driver.field_seed(11, 8.0);
        let map = PointMap::new()
            .with_point(PointId(10), Direction::In, ValueKind::Float)
            .with_point(PointId(11), Direction::In, ValueKind::Float);
        let mut executor = Executor::new(&driver, map, Vec::new()).unwrap();

        executor.scan();

        // The short exchange completed — the boundary counts no
        // exchange failure — but station 1's point escalates to
        // `Disconnected` while station 2's serves the freshly latched
        // image.
        executor.scan();
        let snapshot = executor.snapshot();
        assert_eq!(snapshot.io_health.failed_exchanges, 0);
        assert_eq!(snapshot.io_health.failed_reads, 1);
        assert_eq!(
            snapshot.io_health.last_error,
            Some(IoFault {
                tick: Tick(2),
                point: PointId(10),
                direction: Direction::In,
                error: IoError::Disconnected(PointId(10)),
            })
        );
        assert_eq!(
            snapshot.points[0].sample.unwrap().quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
        assert_eq!(
            snapshot.points[1].sample.unwrap(),
            Sample::good(Value::Float(8.0), Tick(2))
        );

        // The bus link stayed connected — the completed exchange named
        // its shortfall — and the exchange section counts the mismatch.
        let diagnostics = snapshot.io_health.driver.unwrap();
        assert_eq!(diagnostics.link, LinkState::Connected);
        assert_eq!(
            diagnostics.last_error,
            Some("station 1 answered short of its working counter".to_string())
        );
        assert_eq!(diagnostics.exchange.unwrap().working_counter_mismatches, 1);

        // A clean exchange clears the shortfall: both stations serve
        // fresh data again.
        executor.scan();
        assert_eq!(
            executor.snapshot().points[0].sample.unwrap(),
            Sample::good(Value::Float(7.0), Tick(3))
        );
    }

    #[test]
    fn cyclic_diagnostics_report_exchange_counters_and_link() {
        let driver = CyclicStub::new(
            &[(10, 1)],
            3,
            &[
                Exchange::Complete,
                Exchange::Late,
                Exchange::Failed,
                Exchange::Complete,
            ],
        );
        let mut executor = Executor::new(&driver, stale_map(PointId(10), 2), Vec::new()).unwrap();

        executor.run(4);

        // Three of four exchanges completed — the failed one counted
        // once at the boundary — and the late frame reported its missed
        // deadline while still latching.
        let health = executor.snapshot().io_health;
        assert_eq!(health.failed_exchanges, 1);
        let diagnostics = health.driver.unwrap();
        assert_eq!(
            diagnostics.exchange.unwrap(),
            ExchangeDiagnostics {
                attempted: 4,
                succeeded: 3,
                working_counter_mismatches: 0,
                last_exchange_tick: Some(Tick(4)),
                missed_deadlines: 1,
            }
        );
        // The recovered link reports connected again.
        assert_eq!(diagnostics.link, LinkState::Connected);
        assert_eq!(
            driver.exchange_counters().attempted,
            driver.transport_calls()
        );
    }

    #[test]
    fn closed_gate_still_exchanges_but_quiesces_staging() {
        // The gate covers writes, not the exchange: a quiesced standby's
        // cyclic backend keeps latching fresh inputs — and the writes it
        // dropped never staged, so nothing it computed publishes.
        let driver = CyclicStub::new(&[(10, 1), (20, 1)], 3, &[]);
        let gate = crate::WriteGate::closed(&driver);
        let map = PointMap::new()
            .with_point(PointId(10), Direction::In, ValueKind::Float)
            .with_point(PointId(20), Direction::Out, ValueKind::Float);
        let mut executor = Executor::new(
            &gate,
            map,
            vec![Box::new(Constant {
                name: "const",
                output: PointId(20),
                value: 9.0,
            })],
        )
        .unwrap();

        executor.run(2);

        // Both scans exchanged — the gate passed the cyclic surface
        // through — while the quiesced writes never reached the staged
        // image, so the published field stays at its initial value.
        assert_eq!(driver.transport_calls(), 2);
        assert_eq!(
            driver.field_sample(20),
            Some(Sample::good(Value::Float(0.0), Tick::ZERO))
        );
        // The standby's own image still reports what the run computes.
        assert_eq!(
            executor.sample(PointId(20)),
            Some(Sample::good(Value::Float(9.0), Tick(2)))
        );

        // Opening the gate lets the next write stage — and the next
        // exchange publish.
        gate.open();
        executor.scan();
        executor.scan();
        assert_eq!(
            driver.field_sample(20),
            Some(Sample::good(Value::Float(9.0), Tick(4)))
        );
    }

    #[test]
    fn non_cyclic_driver_skips_the_exchange_phase() {
        // A driver without the cyclic surface never sees `exchange`:
        // the counter the cyclic path would increment stays zero.
        let driver = StubDriver::new(&[float(10)], &[]);
        let map: PointMap = [(PointId(10), Direction::In, ValueKind::Float)]
            .into_iter()
            .collect();
        let mut executor = Executor::new(&driver, map, Vec::new()).unwrap();
        executor.run(3);
        assert_eq!(executor.snapshot().io_health.failed_exchanges, 0);
        assert_eq!(executor.snapshot().io_health.driver, None);
    }

    /// A component serving the declared-command surface: `load {to}`
    /// replaces its checkpointed `count` and `bump {by}` adds to it —
    /// `by` defaults to 1, must be at least 1, and is refused once the
    /// count reaches the declared limit, the kind's
    /// `KindDeclared`-availability analogue. `bump`'s standing check is
    /// the `command_refusal` probe's — dispatch and the published
    /// verdict share one predicate. `step` reports the count on
    /// its `Out` `Int` point.
    struct Commanded {
        name: &'static str,
        output: PointId,
        count: i64,
    }

    impl Commanded {
        /// The kind's declared `bump` ceiling.
        const LIMIT: i64 = 100;

        fn commands() -> Vec<CommandDecl> {
            vec![
                CommandDecl {
                    name: "load".to_string(),
                    request: vec![CommandArgument {
                        name: "to".to_string(),
                        kind: ValueKind::Int,
                    }],
                    availability: CommandAvailability::Always,
                },
                CommandDecl {
                    name: "bump".to_string(),
                    request: vec![CommandArgument {
                        name: "by".to_string(),
                        kind: ValueKind::Int,
                    }],
                    availability: CommandAvailability::KindDeclared,
                },
            ]
        }
    }

    impl Component for Commanded {
        fn name(&self) -> &str {
            self.name
        }

        fn io_requirements(&self) -> Vec<IoRequirement> {
            vec![IoRequirement::output::<i64>("count", self.output)]
        }

        fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
            io.write_typed(self.output, self.count)?;
            Ok(())
        }

        fn describe(&self) -> ComponentDescriptor {
            ComponentDescriptor {
                name: self.name.to_string(),
                kind: "commanded".to_string(),
                label: self.name.to_string(),
                ports: vec![PortDescriptor {
                    name: "count".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Int,
                    role: None,
                    point: None,
                }],
                parameters: Vec::new(),
                commands: Self::commands(),
                events: Vec::new(),
            }
        }

        fn command_refusal(&self, command: &str) -> Option<String> {
            match command {
                "bump" if self.count >= Self::LIMIT => {
                    Some("the counter is at its limit".to_string())
                }
                _ => None,
            }
        }

        fn invoke_command(
            &mut self,
            command: &str,
            arguments: &BTreeMap<String, Value>,
        ) -> Result<(), String> {
            match command {
                "load" => {
                    self.count = match arguments.get("to") {
                        Some(Value::Int(to)) => *to,
                        _ => 0,
                    };
                    Ok(())
                }
                "bump" => {
                    if let Some(reason) = self.command_refusal(command) {
                        return Err(reason);
                    }
                    let by = match arguments.get("by") {
                        Some(Value::Int(by)) => *by,
                        None => 1,
                        _ => unreachable!("submission validates the declared argument kind"),
                    };
                    if by < 1 {
                        return Err("by must be at least 1".to_string());
                    }
                    self.count += by;
                    Ok(())
                }
                _ => unreachable!("submission validates the declared command name"),
            }
        }

        fn capture_state(&self) -> StateMap {
            let mut state = StateMap::new();
            state.insert("count", Value::Int(self.count));
            state
        }

        fn restore_state(&mut self, state: &StateMap) -> Result<(), dcs_core::StateError> {
            state.ensure_known_fields(self.name, &["count"])?;
            self.count = state.require_i64(self.name, "count")?;
            Ok(())
        }
    }

    /// A component declaring a `KindDeclared` command its kind neither
    /// probes nor serves: the default `command_refusal` reports it
    /// invocable — the unconditional `available` the read model
    /// published before the section existed — while the default
    /// `invoke_command` still refuses every invocation at dispatch.
    struct Unprobed {
        name: &'static str,
    }

    impl Component for Unprobed {
        fn name(&self) -> &str {
            self.name
        }

        fn io_requirements(&self) -> Vec<IoRequirement> {
            Vec::new()
        }

        fn step(&mut self, _io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
            Ok(())
        }

        fn describe(&self) -> ComponentDescriptor {
            ComponentDescriptor {
                name: self.name.to_string(),
                kind: "unprobed".to_string(),
                label: self.name.to_string(),
                ports: Vec::new(),
                parameters: Vec::new(),
                commands: vec![CommandDecl {
                    name: "ping".to_string(),
                    request: Vec::new(),
                    availability: CommandAvailability::KindDeclared,
                }],
                events: Vec::new(),
            }
        }
    }

    /// A component declaring a command its kind does not serve — the
    /// default `invoke_command` settles every invocation refused.
    struct DeclaresOnly {
        name: &'static str,
    }

    impl Component for DeclaresOnly {
        fn name(&self) -> &str {
            self.name
        }

        fn io_requirements(&self) -> Vec<IoRequirement> {
            Vec::new()
        }

        fn step(&mut self, _io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
            Ok(())
        }

        fn describe(&self) -> ComponentDescriptor {
            ComponentDescriptor {
                name: self.name.to_string(),
                kind: "declares-only".to_string(),
                label: self.name.to_string(),
                ports: Vec::new(),
                parameters: Vec::new(),
                commands: vec![CommandDecl {
                    name: "ping".to_string(),
                    request: Vec::new(),
                    availability: CommandAvailability::Always,
                }],
                events: Vec::new(),
            }
        }
    }

    /// A component emitting declared events during `step`: one `fired`
    /// (`Journal`-retained), one `shift` (`History`-retained), then one
    /// `beat` (`Latest`-retained) per scan — one per declared channel —
    /// the payload's `n` counting emissions — the trio pins
    /// per-component emission order. `fail` reports the step error
    /// after emitting, so the drain-on-failure path is exercised.
    struct Emitter {
        name: &'static str,
        n: i64,
        fail: bool,
    }

    impl Emitter {
        fn event(event: &str, n: i64) -> EmittedEvent {
            EmittedEvent {
                event: event.to_string(),
                component: String::new(),
                fields: [("n".to_string(), EventValue::Value(Value::Int(n)))]
                    .into_iter()
                    .collect(),
            }
        }
    }

    impl Component for Emitter {
        fn name(&self) -> &str {
            self.name
        }

        fn io_requirements(&self) -> Vec<IoRequirement> {
            Vec::new()
        }

        fn step(&mut self, _io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
            self.n += 1;
            if self.fail {
                return Err("the step reported a fault".into());
            }
            Ok(())
        }

        fn describe(&self) -> ComponentDescriptor {
            let event = |name: &str, retention: EventRetention| EventDecl {
                name: name.to_string(),
                payload: vec![EventField {
                    name: "n".to_string(),
                    kind: EventFieldKind::Value(ValueKind::Int),
                    optional: false,
                }],
                retention,
            };
            ComponentDescriptor {
                name: self.name.to_string(),
                kind: "emitter".to_string(),
                label: self.name.to_string(),
                ports: Vec::new(),
                parameters: Vec::new(),
                commands: Vec::new(),
                events: vec![
                    event("fired", EventRetention::Journal),
                    event("shift", EventRetention::History),
                    event("beat", EventRetention::Latest),
                ],
            }
        }

        fn drain_events(&mut self) -> Vec<EmittedEvent> {
            if self.n == 0 {
                return Vec::new();
            }
            vec![
                Self::event("fired", self.n),
                Self::event("shift", self.n),
                Self::event("beat", self.n),
            ]
        }

        fn capture_state(&self) -> StateMap {
            let mut state = StateMap::new();
            state.insert("n", Value::Int(self.n));
            state
        }

        fn restore_state(&mut self, state: &StateMap) -> Result<(), dcs_core::StateError> {
            state.ensure_known_fields(self.name, &["n"])?;
            self.n = state.require_i64(self.name, "n")?;
            Ok(())
        }
    }

    fn invoke(component: &str, command: &str, arguments: &[(&str, Value)]) -> Command {
        Command::Invoke {
            component: component.to_string(),
            command: command.to_string(),
            arguments: arguments
                .iter()
                .map(|(name, value)| (name.to_string(), *value))
                .collect(),
        }
    }

    /// Invoke rig: `Commanded` ("ctr") drives `Out` `Int` point 40 with
    /// its count; `Scale` ("a") rides along as the component declaring
    /// no command surface.
    fn commanded_rig(driver: &StubDriver) -> Executor<'_> {
        let map = PointMap::new()
            .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
            .with_point(PointId(20), Direction::Out, ValueKind::Float)
            .with_point(PointId(40), Direction::Out, ValueKind::Int);
        Executor::new(
            driver,
            map,
            vec![
                Box::new(Commanded {
                    name: "ctr",
                    output: PointId(40),
                    count: 0,
                }),
                Box::new(Scale {
                    name: "a",
                    input: PointId(10),
                    output: PointId(20),
                    gain: 1.0,
                }),
            ],
        )
        .unwrap()
    }

    #[test]
    fn invoke_applies_a_declared_command_at_the_scan_boundary() {
        let driver = StubDriver::new(&[float(10), float(20), int(40)], &[]);
        let mut executor = commanded_rig(&driver);
        let command = invoke("ctr", "bump", &[("by", Value::Int(3))]);

        let receipt = executor.submit_command(command.clone());
        assert_eq!(
            receipt,
            CommandReceipt {
                command,
                outcome: CommandOutcome::Accepted {
                    apply_tick: Tick(1)
                },
                actor: None,
            }
        );
        // Queued, not yet applied: the count still reports its start.
        assert_eq!(driver_value(&driver, 40), Value::Int(0));

        executor.scan();
        assert_eq!(driver_value(&driver, 40), Value::Int(3));
        assert_eq!(
            executor.receipts().last().unwrap().outcome,
            CommandOutcome::Applied { tick: Tick(1) }
        );
    }

    #[test]
    fn invoke_rejections_name_the_component_command_and_argument() {
        let driver = StubDriver::new(&[float(10), float(20), int(40)], &[]);
        let mut executor = commanded_rig(&driver);

        // The component name resolves nothing registered.
        let receipt = executor.submit_command(invoke("nope", "bump", &[]));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::UnknownComponent {
                    component: "nope".to_string()
                }
            }
        );

        // A component declaring no command surface names the command.
        let receipt = executor.submit_command(invoke("a", "bump", &[]));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::UnknownCommand {
                    component: "a".to_string(),
                    command: "bump".to_string(),
                }
            }
        );

        // The declaring component still refuses an undeclared name.
        let receipt = executor.submit_command(invoke("ctr", "spin", &[]));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::UnknownCommand {
                    component: "ctr".to_string(),
                    command: "spin".to_string(),
                }
            }
        );

        // A supplied argument's kind must match its declaration.
        let receipt = executor.submit_command(invoke("ctr", "bump", &[("by", Value::Bool(true))]));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::ArgumentTypeMismatch {
                    component: "ctr".to_string(),
                    command: "bump".to_string(),
                    argument: "by".to_string(),
                    expected: ValueKind::Int,
                    found: ValueKind::Bool,
                }
            }
        );

        // All four refused at admission — nothing reached the queue.
        assert_eq!(executor.receipts().len(), 4);
        assert_eq!(executor.snapshot().command_queue.depth, 0);
    }

    #[test]
    fn invoke_commands_apply_in_submission_order() {
        let driver = StubDriver::new(&[float(10), float(20), int(40)], &[]);
        let mut executor = commanded_rig(&driver);

        // `load` then `bump`: the count lands at 5 + 3.
        executor.submit_command(invoke("ctr", "load", &[("to", Value::Int(5))]));
        executor.submit_command(invoke("ctr", "bump", &[("by", Value::Int(3))]));
        executor.scan();
        assert_eq!(driver_value(&driver, 40), Value::Int(8));
        assert_eq!(
            executor.receipts()[0].outcome,
            CommandOutcome::Applied { tick: Tick(1) }
        );
        assert_eq!(
            executor.receipts()[1].outcome,
            CommandOutcome::Applied { tick: Tick(1) }
        );

        // The swapped order settles differently — submission order, not
        // command kind, decides: `bump` then `load` lands at 5.
        executor.submit_command(invoke("ctr", "bump", &[("by", Value::Int(3))]));
        executor.submit_command(invoke("ctr", "load", &[("to", Value::Int(5))]));
        executor.scan();
        assert_eq!(driver_value(&driver, 40), Value::Int(5));
    }

    #[test]
    fn invoke_boundary_refusals_settle_rejected_with_the_kinds_reason() {
        let driver = StubDriver::new(&[float(10), float(20), int(40)], &[]);
        let mut executor = commanded_rig(&driver);

        // A kind invariant the schema cannot express: `by` below 1.
        let receipt = executor.submit_command(invoke("ctr", "bump", &[("by", Value::Int(0))]));
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        executor.scan();
        assert_eq!(
            executor.receipts().last().unwrap().outcome,
            CommandOutcome::Rejected {
                reason: CommandError::CommandRefused {
                    component: "ctr".to_string(),
                    command: "bump".to_string(),
                    reason: "by must be at least 1".to_string(),
                }
            }
        );
        // A refused invocation changed nothing.
        assert_eq!(driver_value(&driver, 40), Value::Int(0));

        // The declared-availability side: at the limit `bump` is
        // refused while `load` — `Always`-available — still serves.
        executor.submit_command(invoke("ctr", "load", &[("to", Value::Int(100))]));
        executor.submit_command(invoke("ctr", "bump", &[("by", Value::Int(1))]));
        executor.scan();
        assert_eq!(
            executor.receipts()[1].outcome,
            CommandOutcome::Applied { tick: Tick(2) }
        );
        assert_eq!(
            executor.receipts()[2].outcome,
            CommandOutcome::Rejected {
                reason: CommandError::CommandRefused {
                    component: "ctr".to_string(),
                    command: "bump".to_string(),
                    reason: "the counter is at its limit".to_string(),
                }
            }
        );
        assert_eq!(driver_value(&driver, 40), Value::Int(100));
    }

    #[test]
    fn the_default_hook_refuses_a_declared_command() {
        // A kind whose descriptor declares a command it does not serve:
        // the invocation still settles — `command_refused` naming the
        // gap — rather than silently succeeding.
        let driver = StubDriver::new(&[float(10)], &[]);
        let map: PointMap = [(PointId(10), Direction::In, ValueKind::Float)]
            .into_iter()
            .collect();
        let mut executor = Executor::new(
            &driver,
            map,
            vec![Box::new(DeclaresOnly { name: "declares" })],
        )
        .unwrap();

        let receipt = executor.submit_command(invoke("declares", "ping", &[]));
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        executor.scan();
        assert_eq!(
            executor.receipts().last().unwrap().outcome,
            CommandOutcome::Rejected {
                reason: CommandError::CommandRefused {
                    component: "declares".to_string(),
                    command: "ping".to_string(),
                    reason: "the kind does not serve the declared command \"ping\"".to_string(),
                }
            }
        );
    }

    #[test]
    fn invoke_rides_the_bounded_command_queue() {
        // The #365 bound applies unchanged: a validated invoke past the
        // capacity gets the named `queue_full` rejection, and validation
        // still precedes admission on a full queue.
        let driver = StubDriver::new(&[float(10), float(20), int(40)], &[]);
        let mut executor = commanded_rig(&driver).with_command_queue_capacity(1);

        let receipt = executor.submit_command(invoke("ctr", "bump", &[("by", Value::Int(2))]));
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        assert_eq!(executor.snapshot().command_queue.depth, 1);

        // Past the bound: `queue_full` naming no point — the invoke
        // targets a component.
        let receipt = executor.submit_command(invoke("ctr", "bump", &[("by", Value::Int(3))]));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::QueueFull {
                    point: None,
                    capacity: 1,
                }
            }
        );

        // An invalid invoke on the full queue still takes its own named
        // rejection — validation precedes admission.
        let receipt = executor.submit_command(invoke("ctr", "spin", &[]));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::UnknownCommand {
                    component: "ctr".to_string(),
                    command: "spin".to_string(),
                }
            }
        );

        // The scan drains the queue: the admitted invoke applies and
        // settles; admission re-opens.
        executor.scan();
        assert_eq!(driver_value(&driver, 40), Value::Int(2));
        assert_eq!(
            executor.receipts()[0].outcome,
            CommandOutcome::Applied { tick: Tick(1) }
        );
        assert_eq!(executor.snapshot().command_queue.depth, 0);
    }

    #[test]
    fn invoke_effects_ride_the_checkpoint_to_a_restored_component() {
        // `bump` mutates only checkpointed run state: the restored
        // component continues from the adopted count.
        let driver = StubDriver::new(&[float(10), float(20), int(40)], &[]);
        let mut executor = commanded_rig(&driver);
        executor.submit_command(invoke("ctr", "bump", &[("by", Value::Int(7))]));
        executor.scan();
        assert_eq!(driver_value(&driver, 40), Value::Int(7));
        let checkpoint = executor.checkpoint();
        assert_eq!(
            checkpoint.components["ctr"].get("count"),
            Some(Value::Int(7))
        );

        let standby_driver = StubDriver::new(&[float(10), float(20), int(40)], &[]);
        let mut standby = Executor::restore(
            &standby_driver,
            PointMap::new()
                .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
                .with_point(PointId(20), Direction::Out, ValueKind::Float)
                .with_point(PointId(40), Direction::Out, ValueKind::Int),
            vec![
                Box::new(Commanded {
                    name: "ctr",
                    output: PointId(40),
                    count: 0,
                }),
                Box::new(Scale {
                    name: "a",
                    input: PointId(10),
                    output: PointId(20),
                    gain: 1.0,
                }),
            ],
            &checkpoint,
            None,
        )
        .unwrap();

        standby.scan();
        assert_eq!(driver_value(&standby_driver, 40), Value::Int(7));
    }

    #[test]
    fn a_tracking_standby_replays_pending_and_applied_invokes() {
        // Switchover with an invoke still in flight: the checkpoint
        // carries the accepted entry, the tracking standby re-queues it,
        // and its next boundary applies it exactly as the active's did —
        // post-promotion behavior identical to the pre-switchover run.
        let active_driver = StubDriver::new(&[float(10), float(20), int(40)], &[]);
        let mut active = commanded_rig(&active_driver);
        active.submit_command(invoke("ctr", "bump", &[("by", Value::Int(7))]));
        active.scan();
        // Still pending when the checkpoint is taken.
        active.submit_command(invoke("ctr", "bump", &[("by", Value::Int(3))]));
        let checkpoint = active.checkpoint();

        let standby_driver = StubDriver::new(&[float(10), float(20), int(40)], &[]);
        let mut standby = commanded_rig(&standby_driver);
        standby.apply(&checkpoint).unwrap();

        // The standby's boundary replays the carried invoke; the active
        // applies it at its own — the pair's outputs agree.
        active.scan();
        standby.scan();
        assert_eq!(driver_value(&active_driver, 40), Value::Int(10));
        assert_eq!(driver_value(&standby_driver, 40), Value::Int(10));
        assert_eq!(standby.receipts().len(), active.receipts().len());
        assert_eq!(
            standby.receipts().last().unwrap().outcome,
            CommandOutcome::Applied { tick: Tick(2) }
        );
    }

    #[test]
    fn emitted_events_drain_in_scan_and_emission_order() {
        // Two emitters bracket a non-emitting component: the scan's
        // record is component scan order then each component's own
        // emission order, every entry stamped with the producing
        // component's registered name.
        let driver = StubDriver::new(&[float(10), float(20)], &[]);
        let map = PointMap::new()
            .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
            .with_point(PointId(20), Direction::Out, ValueKind::Float);
        let mut executor = Executor::new(
            &driver,
            map,
            vec![
                Box::new(Emitter {
                    name: "first",
                    n: 0,
                    fail: false,
                }),
                Box::new(Scale {
                    name: "a",
                    input: PointId(10),
                    output: PointId(20),
                    gain: 1.0,
                }),
                Box::new(Emitter {
                    name: "second",
                    n: 0,
                    fail: false,
                }),
            ],
        )
        .unwrap();

        assert!(executor.emitted_events().is_empty());
        executor.scan();
        let emitted: Vec<(&str, &str)> = executor
            .emitted_events()
            .iter()
            .map(|event| (event.component.as_str(), event.event.as_str()))
            .collect();
        assert_eq!(
            emitted,
            [
                ("first", "fired"),
                ("first", "shift"),
                ("first", "beat"),
                ("second", "fired"),
                ("second", "shift"),
                ("second", "beat"),
            ]
        );
        assert_eq!(
            executor.emitted_events()[0].fields["n"],
            EventValue::Value(Value::Int(1))
        );

        // The next scan's record replaces the last — the buffer always
        // holds exactly one scan's emissions.
        executor.scan();
        assert_eq!(executor.emitted_events().len(), 6);
        assert_eq!(
            executor.emitted_events()[0].fields["n"],
            EventValue::Value(Value::Int(2))
        );
    }

    #[test]
    fn emitted_events_drain_after_a_failing_step() {
        // The step emitted before reporting its fault: the drain runs
        // on the failure path too, so the scan's record carries the
        // emissions beside the counted step error.
        let driver = StubDriver::new(&[float(10)], &[]);
        let map: PointMap = [(PointId(10), Direction::In, ValueKind::Float)]
            .into_iter()
            .collect();
        let mut executor = Executor::new(
            &driver,
            map,
            vec![Box::new(Emitter {
                name: "em",
                n: 0,
                fail: true,
            })],
        )
        .unwrap();

        executor.scan();
        let emitted: Vec<&str> = executor
            .emitted_events()
            .iter()
            .map(|event| event.event.as_str())
            .collect();
        assert_eq!(emitted, ["fired", "shift", "beat"]);
        let diagnostics = &executor.snapshot().components[0];
        assert_eq!(diagnostics.step_errors, 1);
        assert_eq!(
            diagnostics.last_error.as_deref(),
            Some("the step reported a fault")
        );
    }

    #[test]
    fn a_checkpoint_apply_starts_the_emitted_record_empty() {
        // The drained emissions belong to the abandoned scan line: an
        // `apply` converging the run leaves the record empty rather than
        // replaying events the adopted line never produced.
        let driver = StubDriver::new(&[float(10)], &[]);
        let map: PointMap = [(PointId(10), Direction::In, ValueKind::Float)]
            .into_iter()
            .collect();
        let mut executor = Executor::new(
            &driver,
            map,
            vec![Box::new(Emitter {
                name: "em",
                n: 0,
                fail: false,
            })],
        )
        .unwrap();

        executor.scan();
        assert_eq!(executor.emitted_events().len(), 3);
        let checkpoint = executor.checkpoint();
        executor.apply(&checkpoint).unwrap();
        assert!(executor.emitted_events().is_empty());
    }

    #[test]
    fn drained_emissions_resolve_each_declared_retention_channel() {
        // Every emission the scan drains resolves against the
        // descriptor's declared retention — the channel the serving
        // layer routes the record to: `fired` → `Journal`, `shift` →
        // `History`, `beat` → `Latest`, all three declared channels
        // exercised in one scan.
        let driver = StubDriver::new(&[], &[]);
        let mut executor = emitter_rig(&driver);
        executor.scan();

        let descriptor = &executor.snapshot().descriptors[0];
        let retention_of = |event: &EmittedEvent| {
            descriptor
                .events
                .iter()
                .find(|decl| decl.name == event.event)
                .map(|decl| decl.retention)
        };
        let routed: Vec<(&str, EventRetention)> = executor
            .emitted_events()
            .iter()
            .map(|event| (event.event.as_str(), retention_of(event).unwrap()))
            .collect();
        assert_eq!(
            routed,
            [
                ("fired", EventRetention::Journal),
                ("shift", EventRetention::History),
                ("beat", EventRetention::Latest),
            ]
        );
    }

    /// Emission rig: one `Emitter` ("em") and no I/O surface — the
    /// redundancy pair's proving kind for the pinned standby-emission
    /// semantics.
    fn emitter_rig(driver: &StubDriver) -> Executor<'_> {
        Executor::new(
            driver,
            PointMap::new(),
            vec![Box::new(Emitter {
                name: "em",
                n: 0,
                fail: false,
            })],
        )
        .unwrap()
    }

    #[test]
    fn a_tracking_standby_emits_the_same_declared_events() {
        // The pinned standby-emission semantics: the tracking peer
        // steps the same components on the adopted run state, so every
        // scan's emitted record is identical to the active's — the
        // per-peer event streams are indistinguishable, which is what
        // makes a promoted peer's journal an uninterrupted run's
        // journal.
        let active_driver = StubDriver::new(&[], &[]);
        let mut active = emitter_rig(&active_driver);
        let standby_driver = StubDriver::new(&[], &[]);
        let mut standby = emitter_rig(&standby_driver);

        // A standby joining mid-run: the checkpointed component state
        // aligns the emission sequence, so its first tracked scan
        // already emits what the active's does.
        active.run(3);
        standby.apply(&active.checkpoint()).unwrap();

        // The driven pair's cycle: adopt the active's post-scan state,
        // then scan — the peers' emitted records are equal scan by
        // scan, whether or not the pull lands between them.
        for _ in 0..4 {
            standby.scan();
            active.scan();
            assert_eq!(standby.emitted_events(), active.emitted_events());
            standby.apply(&active.checkpoint()).unwrap();
        }
    }

    #[test]
    fn post_promotion_emissions_continue_the_adopted_sequence() {
        // Once the tracking pull stops — the peer owns the field — the
        // stream is the adopted run's own continuation: no reset, no
        // replay of the abandoned line's record, indistinguishable from
        // an uninterrupted reference for every scan asserted.
        let reference_driver = StubDriver::new(&[], &[]);
        let mut reference = emitter_rig(&reference_driver);
        let standby_driver = StubDriver::new(&[], &[]);
        let mut standby = emitter_rig(&standby_driver);

        for _ in 0..3 {
            standby.apply(&reference.checkpoint()).unwrap();
            standby.scan();
            reference.scan();
        }
        // Promotion: the peer stops applying and runs on.
        for _ in 0..4 {
            standby.scan();
            reference.scan();
            assert_eq!(standby.emitted_events(), reference.emitted_events());
        }
    }

    #[test]
    fn an_adopted_pending_invoke_settles_once_and_stays_settled() {
        // Exactly-once across adoption: the checkpoint's `Accepted`
        // entry re-queues once on the applying peer and settles at its
        // next boundary; a later checkpoint carrying the settled
        // outcome never re-queues it — the receipt log is the pair's
        // one audit, not a replay channel.
        let active_driver = StubDriver::new(&[float(10), float(20), int(40)], &[]);
        let mut active = commanded_rig(&active_driver);
        active.submit_command(invoke("ctr", "bump", &[("by", Value::Int(7))]));
        let pending = active.checkpoint();

        let standby_driver = StubDriver::new(&[float(10), float(20), int(40)], &[]);
        let mut standby = commanded_rig(&standby_driver);
        standby.apply(&pending).unwrap();
        standby.scan();

        assert_eq!(standby.receipts().len(), 1);
        assert_eq!(
            standby.receipts()[0].outcome,
            CommandOutcome::Applied { tick: Tick(1) }
        );
        assert_eq!(driver_value(&standby_driver, 40), Value::Int(7));

        // The settled outcome rides the next checkpoint forward — the
        // re-application lands nothing: still one receipt, still 7.
        active.scan();
        standby.apply(&active.checkpoint()).unwrap();
        standby.scan();
        assert_eq!(standby.receipts().len(), 1);
        assert_eq!(driver_value(&standby_driver, 40), Value::Int(7));
    }

    /// Reads `command`'s published verdict on `component` out of the
    /// snapshot's `command_verdicts` section — `None` when the
    /// component or command carries none.
    fn verdict<'a>(
        snapshot: &'a TelemetrySnapshot,
        component: &str,
        command: &str,
    ) -> Option<&'a CommandVerdict> {
        snapshot
            .command_verdicts
            .iter()
            .find(|entry| entry.name == component)
            .and_then(|entry| entry.verdicts.iter().find(|v| v.name == command))
    }

    #[test]
    fn the_post_scan_probe_publishes_kind_declared_verdicts() {
        // `Commanded`'s `bump` is `KindDeclared`: at the limit the
        // probe reports the standing refusal the receipted path would
        // settle. `load` is `Always` and takes no verdict — the
        // section covers exactly the declared `KindDeclared` commands.
        let driver = StubDriver::new(&[float(10), float(20), int(40)], &[]);
        let mut executor = commanded_rig(&driver);

        // A scan product: before the first scan the section is empty.
        assert!(executor.snapshot().command_verdicts.is_empty());

        executor.scan();
        let snapshot = executor.snapshot();
        // One entry per registered component, in scan order; `a`
        // declares no command surface, so its verdict list is empty.
        assert_eq!(
            snapshot
                .command_verdicts
                .iter()
                .map(|entry| entry.name.as_str())
                .collect::<Vec<_>>(),
            ["ctr", "a"]
        );
        assert_eq!(
            snapshot.command_verdicts[0].verdicts,
            [CommandVerdict {
                name: "bump".to_string(),
                available: true,
                refusal: None,
            }]
        );
        assert!(snapshot.command_verdicts[1].verdicts.is_empty());

        // Drive the count to the limit: the same scan's probe reports
        // `bump`'s standing refusal — the text dispatch would settle.
        executor.submit_command(invoke("ctr", "load", &[("to", Value::Int(100))]));
        executor.scan();
        let snapshot = executor.snapshot();
        assert_eq!(
            verdict(&snapshot, "ctr", "bump"),
            Some(&CommandVerdict {
                name: "bump".to_string(),
                available: false,
                refusal: Some("the counter is at its limit".to_string()),
            })
        );

        // `load` back under the limit re-opens `bump` on the next
        // scan's probe.
        executor.submit_command(invoke("ctr", "load", &[("to", Value::Int(0))]));
        executor.scan();
        assert_eq!(
            verdict(&executor.snapshot(), "ctr", "bump"),
            Some(&CommandVerdict {
                name: "bump".to_string(),
                available: true,
                refusal: None,
            })
        );
    }

    #[test]
    fn probe_and_dispatch_share_the_standing_predicate() {
        // The proving kind's standing check is one code path: at the
        // limit the probe's answer is the reason dispatch settles, and
        // both report the `Always`-available `load` invocable
        // throughout.
        let mut commanded = Commanded {
            name: "ctr",
            output: PointId(40),
            count: 0,
        };
        let no_arguments = BTreeMap::new();
        assert_eq!(commanded.command_refusal("bump"), None);
        assert!(commanded.invoke_command("bump", &no_arguments).is_ok());

        commanded.count = Commanded::LIMIT;
        let standing = commanded.command_refusal("bump");
        assert_eq!(standing.as_deref(), Some("the counter is at its limit"));
        assert_eq!(
            commanded.invoke_command("bump", &no_arguments).unwrap_err(),
            standing.unwrap()
        );
        // `load` stands invocable — probe and dispatch agree.
        assert_eq!(commanded.command_refusal("load"), None);
        let to_zero: BTreeMap<String, Value> =
            [("to".to_string(), Value::Int(0))].into_iter().collect();
        assert!(commanded.invoke_command("load", &to_zero).is_ok());
        assert_eq!(commanded.count, 0);
    }

    #[test]
    fn a_kind_without_a_probe_reports_nothing_new() {
        // The default `command_refusal` reports every declared command
        // invocable — the unconditional `available` the read model
        // published before the section existed — so a kind that never
        // overrides it carries the same reporting it always did.
        let driver = StubDriver::new(&[float(10)], &[]);
        let map: PointMap = [(PointId(10), Direction::In, ValueKind::Float)]
            .into_iter()
            .collect();
        let mut executor =
            Executor::new(&driver, map, vec![Box::new(Unprobed { name: "unprobed" })]).unwrap();

        executor.scan();
        assert_eq!(
            executor.snapshot().command_verdicts,
            [ComponentCommands {
                name: "unprobed".to_string(),
                verdicts: vec![CommandVerdict {
                    name: "ping".to_string(),
                    available: true,
                    refusal: None,
                }],
            }]
        );

        // The verdict is advisory, never a second authority: a
        // submission the probe calls available still validates, queues,
        // and settles through the receipted path — here the default
        // invoke hook's refusal — and the disagreement fails nothing.
        let receipt = executor.submit_command(invoke("unprobed", "ping", &[]));
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        executor.scan();
        assert_eq!(
            executor.receipts().last().unwrap().outcome,
            CommandOutcome::Rejected {
                reason: CommandError::CommandRefused {
                    component: "unprobed".to_string(),
                    command: "ping".to_string(),
                    reason: "the kind does not serve the declared command \"ping\"".to_string(),
                }
            }
        );
        // And the verdict itself is untouched by the refused dispatch.
        assert_eq!(
            verdict(&executor.snapshot(), "unprobed", "ping"),
            Some(&CommandVerdict {
                name: "ping".to_string(),
                available: true,
                refusal: None,
            })
        );
    }

    #[test]
    fn a_tracking_standby_publishes_identical_verdicts() {
        // The verdicts are a pure function of component state evaluated
        // inside the scan, so a tracking peer re-derives the active's
        // verdicts from the adopted state — identical sections scan by
        // scan, like the emitted record.
        let active_driver = StubDriver::new(&[float(10), float(20), int(40)], &[]);
        let mut active = commanded_rig(&active_driver);
        let standby_driver = StubDriver::new(&[float(10), float(20), int(40)], &[]);
        let mut standby = commanded_rig(&standby_driver);

        // The active's count reaches the limit before the standby
        // joins: the adopted state carries `bump`'s standing refusal.
        active.submit_command(invoke("ctr", "load", &[("to", Value::Int(100))]));
        active.scan();
        standby.apply(&active.checkpoint()).unwrap();
        // The abandoned line's verdicts do not linger past the apply:
        // the adopted line has completed no scan yet.
        assert!(standby.snapshot().command_verdicts.is_empty());

        for _ in 0..4 {
            standby.scan();
            active.scan();
            assert_eq!(
                standby.snapshot().command_verdicts,
                active.snapshot().command_verdicts
            );
            standby.apply(&active.checkpoint()).unwrap();
        }
        // Both report `bump`'s standing refusal — the adopted state
        // carries the limit.
        standby.scan();
        assert_eq!(
            verdict(&standby.snapshot(), "ctr", "bump").and_then(|v| v.refusal.as_deref()),
            Some("the counter is at its limit")
        );

        // A carried `Accepted` invocation settles at the standby's own
        // boundary and the same scan's verdicts follow the effect:
        // `load` below the limit re-opens `bump` on both peers.
        active.submit_command(invoke("ctr", "load", &[("to", Value::Int(0))]));
        standby.apply(&active.checkpoint()).unwrap();
        active.scan();
        standby.scan();
        assert_eq!(
            standby.snapshot().command_verdicts,
            active.snapshot().command_verdicts
        );
        assert_eq!(
            verdict(&standby.snapshot(), "ctr", "bump").map(|v| v.available),
            Some(true)
        );
    }
}
