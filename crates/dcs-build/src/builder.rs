//! [`PlantBuilder`]: composes a [`PlantModel`] document from typed parts.
//!
//! The builder records the same collections the model document carries —
//! devices, logical I/O points, signals, component instances, and
//! connections — but hands back typed handles at every step, so the
//! shape of the emitted document is constrained while it is composed:
//!
//! - [`channel`](PlantBuilder::channel) declares a device channel of a
//!   [`PointType`] and direction;
//! - [`field_input`](PlantBuilder::field_input) and friends declare an
//!   I/O point and return an [`InPoint<T>`]/[`OutPoint<T>`] handle
//!   carrying its value type;
//! - [`add`](PlantBuilder::add) registers a component from its
//!   [`Spec`](crate::Spec) and returns typed port handles;
//! - [`connect`](PlantBuilder::connect) accepts only a producing
//!   `Source<T>` into a consuming `Sink<T>` — wrong-direction and
//!   wrong-kind connections fail to compile.
//!
//! What the types cannot express, [`build`](PlantBuilder::build) checks
//! with named [`BuildError`]s: every spec-declared required parameter
//! present, no undeclared or mistyped parameters, and — through the same
//! [`validate`](PlantModel::validate) a loaded document faces — every
//! id, port name, channel reference, direction, and value kind in the
//! document resolving.

use crate::endpoint::{InPoint, OutPoint, Sink, Source};
use crate::spec::{ParamDecl, PortDecl, Spec};
use dcs_core::{Direction, PointId, PointType, SignalId, Value, ValueKind};
use dcs_model::{
    Channel, ChannelRef, ComponentId, ComponentInstance, Connection, Device, DeviceId, Equipment,
    IoPoint, MODEL_VERSION, PlantModel, Port, RecordingDuty, Signal, ValidationError,
};
use std::collections::BTreeMap;
use std::fmt;

/// Why [`PlantBuilder::build`] could not emit a valid document.
///
/// The parameter variants come from the spec's declared parameter set;
/// [`Invalid`](Self::Invalid) carries the same
/// [`ValidationError`]s [`PlantModel::load`] would report — the emitted
/// document cannot fail a check a hand-written document would pass.
#[derive(Debug, Clone, PartialEq)]
pub enum BuildError {
    /// An equipment composition's configuration cannot produce a valid
    /// model, detected before it mutates the builder.
    InvalidConfiguration {
        /// The configuration field that could not be used.
        field: String,
        /// Why the value is invalid.
        reason: String,
    },
    /// An instance's parameter map omits a parameter its spec declares
    /// required.
    MissingParameter {
        /// The component missing the parameter.
        component: ComponentId,
        /// The missing parameter's name.
        parameter: String,
    },
    /// An instance's parameter map names a parameter its spec does not
    /// declare — almost always a misspelled key, since an undeclared key
    /// is dead configuration the kind's `from_parameters` never reads.
    UnknownParameter {
        /// The component carrying the parameter.
        component: ComponentId,
        /// The undeclared parameter's name.
        parameter: String,
    },
    /// A supplied parameter's value kind differs from the spec's
    /// declared kind.
    ParameterKindMismatch {
        /// The component carrying the parameter.
        component: ComponentId,
        /// The offending parameter's name.
        parameter: String,
        /// The kind the spec declares.
        expected: ValueKind,
        /// The kind the supplied value carries.
        found: ValueKind,
    },
    /// A supplied parameter's value falls outside the spec's declared
    /// [`ParameterRange`](dcs_core::ParameterRange).
    ParameterOutOfRange {
        /// The component carrying the parameter.
        component: ComponentId,
        /// The offending parameter's name.
        parameter: String,
        /// The supplied value.
        value: Value,
    },
    /// The composed document fails the structural validation
    /// [`PlantModel::load`] runs — dangling or duplicate ids, unresolved
    /// channel references, mismatched directions or value kinds on
    /// points, channels, and connection ends.
    Invalid(Vec<ValidationError>),
}

impl fmt::Display for BuildError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidConfiguration { field, reason } => {
                write!(f, "invalid configuration {field:?}: {reason}")
            }
            Self::MissingParameter {
                component,
                parameter,
            } => write!(
                f,
                "component {} requires parameter {parameter:?}",
                component.0
            ),
            Self::UnknownParameter {
                component,
                parameter,
            } => write!(
                f,
                "component {} has undeclared parameter {parameter:?}",
                component.0
            ),
            Self::ParameterKindMismatch {
                component,
                parameter,
                expected,
                found,
            } => write!(
                f,
                "component {} parameter {parameter:?} requires {expected:?}, found {found:?}",
                component.0
            ),
            Self::ParameterOutOfRange {
                component,
                parameter,
                value,
            } => write!(
                f,
                "component {} parameter {parameter:?} is out of its declared range: {value:?}",
                component.0
            ),
            Self::Invalid(errors) => {
                write!(f, "invalid plant model ({} error(s)):", errors.len())?;
                for error in errors {
                    write!(f, " {error};")?;
                }
                Ok(())
            }
        }
    }
}

impl std::error::Error for BuildError {}

/// Composes a versioned [`PlantModel`] document from typed handles.
///
/// See the module docs for the composition flow; the emitted document is
/// exactly the document `dcs-model` loads and validates — the builder
/// adds no fields and no schema of its own.
///
/// Device and component ids allocate sequentially from 1 in declaration
/// order; point and signal ids are supplied explicitly, since they are
/// the plant's engineering identifiers.
pub struct PlantBuilder {
    devices: Vec<Device>,
    io_points: Vec<IoPoint>,
    signals: Vec<Signal>,
    components: Vec<ComponentInstance>,
    connections: Vec<Connection>,
    equipment: Vec<Equipment>,
    views: Vec<dcs_model::PlantView>,
    /// Each component's spec-declared parameter set, for `build`'s
    /// checks; `None` marks a spec leaving its parameters unchecked.
    parameter_sets: Vec<(ComponentId, Option<Vec<ParamDecl>>)>,
    /// Each component's spec-declared port set — a spec-declared
    /// `PortDecl::unit` is the port's inherent dimension for `connect`/
    /// `port_unit`'s agreement checks; the emitted `Port::unit` records
    /// composition declarations only, so an unchanged composition emits
    /// identical bytes across a compatible crossing.
    port_sets: Vec<(ComponentId, Vec<PortDecl>)>,
    next_device: u64,
    next_component: u64,
}

impl Default for PlantBuilder {
    fn default() -> Self {
        Self::new()
    }
}

impl PlantBuilder {
    /// An empty composition.
    pub fn new() -> Self {
        Self {
            devices: Vec::new(),
            io_points: Vec::new(),
            signals: Vec::new(),
            components: Vec::new(),
            connections: Vec::new(),
            equipment: Vec::new(),
            views: Vec::new(),
            parameter_sets: Vec::new(),
            port_sets: Vec::new(),
            next_device: 1,
            next_component: 1,
        }
    }

    /// Registers an equipment's ownership and operator surface using the
    /// already declared component and point identities. [`build`](Self::build)
    /// validates references, exclusive ownership, and writable controls.
    pub fn equipment(&mut self, equipment: Equipment) -> &mut Self {
        self.equipment.push(equipment);
        self
    }

    /// Registers a code-engineered process drawing. [`build`](Self::build)
    /// validates references, geometry, pipework, and area navigation.
    pub fn view(&mut self, view: dcs_model::PlantView) -> &mut Self {
        self.views.push(view);
        self
    }

    /// Declares a field or simulated I/O device of `kind` and returns it
    /// for channel and parameter setup: the device's id allocates
    /// sequentially and `channels`/`parameters` fill in place.
    ///
    /// ```rust
    /// let mut plant = dcs_build::PlantBuilder::new();
    /// let sim = plant.device("sim").id;
    /// ```
    pub fn device(&mut self, kind: &str) -> &mut Device {
        let id = DeviceId(self.next_device);
        self.next_device += 1;
        self.devices.push(Device {
            id,
            kind: kind.to_string(),
            channels: BTreeMap::new(),
            hardware: false,
            parameters: BTreeMap::new(),
        });
        self.devices.last_mut().unwrap()
    }

    /// The device `id` names, mutable — the crate-internal accessor the
    /// device-kind surfaces (e.g. [`ethercat`](crate::ethercat)) use to
    /// populate a declared device's `parameters`.
    pub(crate) fn device_mut(&mut self, id: DeviceId) -> Option<&mut Device> {
        self.devices.iter_mut().find(|device| device.id == id)
    }

    /// Declares a channel of `name` on `device`, carrying `direction`
    /// and `T`'s value kind, and returns the [`ChannelRef`] points bind.
    ///
    /// # Panics
    ///
    /// `device` must come from this builder's [`device`](Self::device)
    /// — a foreign or fabricated id is a programming error.
    pub fn channel<T: PointType>(
        &mut self,
        device: DeviceId,
        name: &str,
        direction: Direction,
    ) -> ChannelRef {
        let Some(device) = self.devices.iter_mut().find(|d| d.id == device) else {
            panic!("channel {name:?} names a device this builder did not declare");
        };
        device.channels.insert(
            name.to_string(),
            Channel {
                direction,
                value_type: T::KIND,
            },
        );
        ChannelRef {
            device: device.id,
            name: name.to_string(),
        }
    }

    /// Declares an `In` point of value type `T` bound to `channel`,
    /// returning the producing [`InPoint<T>`] handle.
    ///
    /// `writable` marks the point an operator command target. The
    /// channel binding resolves at [`build`](Self::build): an unknown
    /// device or channel, or a channel whose declared direction or kind
    /// disagrees, is a named [`BuildError::Invalid`].
    pub fn field_input<T: PointType>(
        &mut self,
        id: PointId,
        channel: ChannelRef,
        writable: bool,
    ) -> InPoint<T> {
        self.io_points.push(IoPoint {
            id,
            direction: Direction::In,
            value_type: T::KIND,
            channel: Some(channel),
            initial: None,
            writable,
            requires_reason: false,
            stale_after_ticks: None,
            journaled: false,
            record: None,
            unit: None,
        });
        InPoint::new(id)
    }

    /// Declares an `In` point of value type `T` bound to `channel` with
    /// a declared freshness budget of `stale_after_ticks` ticks — like
    /// [`field_input`](Self::field_input), plus the executor landing the
    /// image sample as `Uncertain(Stale)` once the driver-stamped tick
    /// lags the scan tick by more than the budget. A budget of `0`
    /// requires a sample stamped at the current scan tick.
    pub fn field_input_stale_after<T: PointType>(
        &mut self,
        id: PointId,
        channel: ChannelRef,
        writable: bool,
        stale_after_ticks: u64,
    ) -> InPoint<T> {
        let point = self.field_input(id, channel, writable);
        self.io_points.last_mut().unwrap().stale_after_ticks = Some(stale_after_ticks);
        point
    }

    /// Declares an `Out` point of value type `T` bound to `channel`,
    /// returning the consuming [`OutPoint<T>`] handle.
    ///
    /// The channel binding resolves at [`build`](Self::build), like
    /// [`field_input`](Self::field_input). `Out` points are never
    /// `writable`: the command path refuses them outright.
    pub fn field_output<T: PointType>(&mut self, id: PointId, channel: ChannelRef) -> OutPoint<T> {
        self.io_points.push(IoPoint {
            id,
            direction: Direction::Out,
            value_type: T::KIND,
            channel: Some(channel),
            initial: None,
            writable: false,
            requires_reason: false,
            stale_after_ticks: None,
            journaled: false,
            record: None,
            unit: None,
        });
        OutPoint::new(id)
    }

    /// Declares an `In` point the scan image carries rather than the
    /// field — a held operator value — seeded with `initial`, returning
    /// the producing [`InPoint<T>`] handle.
    ///
    /// `writable` marks the point an operator command target: a writable
    /// internal `In` point holds the written value until the next
    /// command, the common target for setpoints.
    pub fn internal_input<T: PointType>(
        &mut self,
        id: PointId,
        initial: T,
        writable: bool,
    ) -> InPoint<T> {
        self.io_points.push(IoPoint {
            id,
            direction: Direction::In,
            value_type: T::KIND,
            channel: None,
            initial: Some(initial.into_value()),
            writable,
            requires_reason: false,
            stale_after_ticks: None,
            journaled: false,
            record: None,
            unit: None,
        });
        InPoint::new(id)
    }

    /// Declares an `Out` point the scan image carries — an observed
    /// internal or point-to-point carrier — seeded with `initial`,
    /// returning the consuming [`OutPoint<T>`] handle.
    pub fn internal_output<T: PointType>(&mut self, id: PointId, initial: T) -> OutPoint<T> {
        self.io_points.push(IoPoint {
            id,
            direction: Direction::Out,
            value_type: T::KIND,
            channel: None,
            initial: Some(initial.into_value()),
            writable: false,
            requires_reason: false,
            stale_after_ticks: None,
            journaled: false,
            record: None,
            unit: None,
        });
        OutPoint::new(id)
    }

    /// Marks a declared point `journaled`: its observed value
    /// transitions join the durable journal as `point_changed` entries
    /// — the declared record flag for the status, lifecycle, mode, and
    /// protection points the lifecycle-audit decision names.
    ///
    /// `point` may be a bare [`PointId`] or a handle a point declaration
    /// returned — either direction, field or internal. Marking an id the
    /// builder never declared is a programming error and panics naming
    /// it; marking a `Float` point surfaces as
    /// [`BuildError::Invalid`] at [`build`](Self::build), where the same
    /// validation a loaded document faces rejects it.
    pub fn journaled(&mut self, point: impl Into<PointId>) -> &mut Self {
        let point = point.into();
        let declared = self
            .io_points
            .iter_mut()
            .find(|declared| declared.id == point)
            .unwrap_or_else(|| panic!("journaled names undeclared io_point {}", point.0));
        declared.journaled = true;
        self
    }

    /// Marks a declared point `requires_reason`: commands against it
    /// must carry a declared `reason` on the attributed envelope —
    /// refusing `reason_required` at admission without one — the
    /// per-alarm mandatory-reason declaration the shelving-reason
    /// decision records for managed request points.
    ///
    /// `point` may be a bare [`PointId`] or a handle a point declaration
    /// returned. Marking an id the builder never declared is a
    /// programming error and panics naming it; marking anything but a
    /// writable `In` point surfaces as [`BuildError::Invalid`] at
    /// [`build`](Self::build), where the same validation a loaded
    /// document faces rejects the dead declaration.
    pub fn requires_reason(&mut self, point: impl Into<PointId>) -> &mut Self {
        let point = point.into();
        let declared = self
            .io_points
            .iter_mut()
            .find(|declared| declared.id == point)
            .unwrap_or_else(|| panic!("requires_reason names undeclared io_point {}", point.0));
        declared.requires_reason = true;
        self
    }

    /// Marks a declared point's durable recording duty: the monitor's
    /// recorder samples its post-scan image into the durable history
    /// file every `every_ticks` run ticks — the durable-history
    /// decision's declared-duty narrowing of the volatile-ring rule,
    /// so a compliance series (the per-filter turbidity record, the
    /// daily disinfection/CT record, interval energy data) persists
    /// across restart rather than living only in the diagnostic
    /// window.
    ///
    /// `point` may be a bare [`PointId`] or a handle a point
    /// declaration returned — either direction, field or internal.
    /// Marking an id the builder never declared is a programming
    /// error and panics naming it; a zero `every_ticks` — every scan
    /// recording, the full-rate stream the durable record exists to
    /// avoid — surfaces as [`BuildError::Invalid`] at
    /// [`build`](Self::build), where the same validation a loaded
    /// document faces rejects the dead declaration.
    pub fn record(&mut self, point: impl Into<PointId>, every_ticks: u64) -> &mut Self {
        self.record_duty(point, every_ticks, None)
    }

    /// Like [`record`](Self::record), plus the declared retention span
    /// the downstream records system must hold, in days — the
    /// deployment-sizing half of the duty: the durable file's growth
    /// is a deployment-managed bound, so `retain_days` is engineering
    /// data a deployment sizes, rotates, and archives against, never
    /// a bound the controller enforces.
    pub fn record_retained(
        &mut self,
        point: impl Into<PointId>,
        every_ticks: u64,
        retain_days: u64,
    ) -> &mut Self {
        self.record_duty(point, every_ticks, Some(retain_days))
    }

    /// Shared `record`/`record_retained` body: the mark lands on the
    /// declared point or panics naming the undeclared id.
    fn record_duty(
        &mut self,
        point: impl Into<PointId>,
        every_ticks: u64,
        retain_days: Option<u64>,
    ) -> &mut Self {
        let point = point.into();
        let declared = self
            .io_points
            .iter_mut()
            .find(|declared| declared.id == point)
            .unwrap_or_else(|| panic!("record names undeclared io_point {}", point.0));
        declared.record = Some(RecordingDuty {
            every_ticks,
            retain_days,
        });
        self
    }

    /// Declares the engineering unit a point's value is expressed in —
    /// [`crate::unit::M3_PER_H`], [`crate::unit::MG_PER_L`]: the
    /// dimensional half of the point's contract, distinct from
    /// `value_type`'s representation. A connection joining the point to
    /// an end declaring a different unit fails [`connect`](Self::connect)
    /// where both declarations are already known, and surfaces as
    /// [`BuildError::Invalid`] — the model's
    /// [`ConnectionUnitMismatch`](dcs_model::ValidationError::ConnectionUnitMismatch)
    /// — at [`build`](Self::build) where they are not. A signal
    /// sourcing the point inherits the declaration at `build` unless it
    /// declares its own, and a disagreeing explicit declaration is
    /// rejected the same way.
    ///
    /// `point` may be a bare [`PointId`] or a handle a point
    /// declaration returned — either direction, field or internal.
    /// Naming an id the builder never declared is a programming error
    /// and panics naming it, and so is declaring a second unit that
    /// disagrees with the first.
    ///
    /// [`crate::unit::DIMENSIONLESS`] declares the point deliberately
    /// unitless — a checked declaration distinct from leaving `unit`
    /// unset, which stays uncheckable on connections.
    pub fn unit(&mut self, point: impl Into<PointId>, unit: &str) -> &mut Self {
        let point = point.into();
        let declared = self
            .io_points
            .iter_mut()
            .find(|declared| declared.id == point)
            .unwrap_or_else(|| panic!("unit names undeclared io_point {}", point.0));
        if let Some(existing) = &declared.unit
            && existing != unit
        {
            panic!("io_point {} already declares unit {existing:?}", point.0);
        }
        declared.unit = Some(unit.to_string());
        self
    }

    /// Declares the engineering unit a component instance's named port
    /// carries — the composition-level dimensional declaration for a
    /// kind whose port is unit-transparent: a `flow-paced-ratio`'s
    /// `flow` is whatever flow the plant wires, so the instance —
    /// not the spec table — declares `"m3/h"` here. A port whose spec
    /// already declares an inherent unit
    /// ([`PortDecl::unit`](crate::PortDecl::unit)) is fixed by the kind:
    /// declaring a disagreeing unit is a programming error and panics.
    ///
    /// `component`/`port` must name a registered instance and a port it
    /// declares — anything else panics naming it. The declaration lands
    /// on the emitted [`Port`](dcs_model::Port)'s `unit` and faces the
    /// same connection checks as a point's.
    pub fn port_unit(&mut self, component: ComponentId, port: &str, unit: &str) -> &mut Self {
        let instance = self
            .components
            .iter_mut()
            .find(|instance| instance.id == component)
            .unwrap_or_else(|| panic!("port_unit names undeclared component {}", component.0));
        let declared = instance.ports.get_mut(port).unwrap_or_else(|| {
            panic!(
                "port_unit names undeclared port {port:?} on component {}",
                component.0
            )
        });
        if let Some(existing) = &declared.unit
            && existing != unit
        {
            panic!(
                "component {} port {port:?} already declares unit {existing:?}",
                component.0
            );
        }
        if let Some(Some(spec_unit)) = self
            .port_sets
            .iter()
            .find(|(id, _)| *id == component)
            .and_then(|(_, decls)| decls.iter().find(|decl| decl.name == port))
            .map(|decl| decl.unit.as_deref())
            && spec_unit != unit
        {
            panic!(
                "component {} port {port:?} spec declares unit {spec_unit:?}",
                component.0
            );
        }
        declared.unit = Some(unit.to_string());
        self
    }

    /// Declares the engineering unit a component instance's named
    /// parameter is expressed in — the composition-level declaration
    /// for a unit-transparent parameter: a `latching-alarm`'s
    /// `low_limit` is in whatever the wired `input` carries, so the
    /// instance declares `"L"` here. A parameter whose spec already
    /// declares an inherent unit
    /// ([`ParamDecl::unit`](crate::ParamDecl::unit)) is fixed by the
    /// kind: declaring a disagreeing unit is a programming error and
    /// panics, as is naming a parameter the spec does not declare or
    /// the instance's map does not supply — a unit must hang on a
    /// declared value.
    ///
    /// `component`/`parameter` must name a registered instance and a
    /// parameter it carries — anything else panics naming it. The
    /// declaration lands in the instance's `parameter_units` beside the
    /// value in the emitted document.
    pub fn param_unit(&mut self, component: ComponentId, parameter: &str, unit: &str) -> &mut Self {
        let index = self
            .components
            .iter()
            .position(|instance| instance.id == component)
            .unwrap_or_else(|| panic!("param_unit names undeclared component {}", component.0));
        let instance = &mut self.components[index];
        if !instance.parameters.contains_key(parameter) {
            panic!(
                "param_unit names parameter {parameter:?} component {} does not carry",
                component.0
            );
        }
        if let Some(existing) = instance.parameter_units.get(parameter)
            && existing != unit
        {
            panic!(
                "component {} parameter {parameter:?} already declares unit {existing:?}",
                component.0
            );
        }
        if let Some(Some(declared)) = self
            .parameter_sets
            .iter()
            .find(|(id, _)| *id == component)
            .map(|(_, decls)| decls.as_ref())
        {
            match declared.iter().find(|decl| decl.name == parameter) {
                None => panic!(
                    "param_unit names parameter {parameter:?} component {} does not declare",
                    component.0
                ),
                Some(decl) => {
                    if let Some(spec_unit) = decl.unit
                        && spec_unit != unit
                    {
                        panic!(
                            "component {} parameter {parameter:?} spec declares unit {spec_unit:?}",
                            component.0
                        );
                    }
                }
            }
        }
        instance
            .parameter_units
            .insert(parameter.to_string(), unit.to_string());
        self
    }

    /// The unit an endpoint currently declares — `Some(unit)` where the
    /// endpoint resolves, `None` where it names an element the builder
    /// does not yet know (a [`Dynamic`](crate::Dynamic) end's document
    /// resolves it at [`build`](Self::build)). A port's spec-declared
    /// `PortDecl::unit` is authoritative — a fixed-by-the-kind
    /// dimension the composition cannot overrule — beside the emitted
    /// [`Port::unit`](dcs_model::Port)'s composition declaration.
    fn declared_unit(&self, endpoint: &dcs_model::Endpoint) -> Option<Option<&str>> {
        match endpoint {
            dcs_model::Endpoint::Point(id) => self
                .io_points
                .iter()
                .find(|point| point.id == *id)
                .map(|point| point.unit.as_deref()),
            dcs_model::Endpoint::Port(port_ref) => {
                if let Some(Some(unit)) = self
                    .port_sets
                    .iter()
                    .find(|(id, _)| *id == port_ref.component)
                    .and_then(|(_, decls)| decls.iter().find(|decl| decl.name == port_ref.name))
                    .map(|decl| decl.unit.as_deref())
                {
                    return Some(Some(unit));
                }
                self.components
                    .iter()
                    .find(|instance| instance.id == port_ref.component)
                    .and_then(|instance| instance.ports.get(&port_ref.name))
                    .map(|port| port.unit.as_deref())
            }
        }
    }

    /// Declares a plant signal named `name` sourcing `source` — a point
    /// id or a point handle — and returns a builder for its optional
    /// display metadata (`unit`, `description`, `group`).
    ///
    /// A `source` naming no declared point is `ValidationError`-
    /// `UnknownSource` at [`build`](Self::build).
    pub fn signal(
        &mut self,
        id: SignalId,
        name: &str,
        source: impl Into<PointId>,
    ) -> SignalBuilder<'_> {
        self.signals.push(Signal {
            id,
            name: name.to_string(),
            source: source.into(),
            unit: None,
            description: None,
            group: None,
        });
        SignalBuilder {
            signal: self.signals.last_mut().unwrap(),
        }
    }

    /// Registers a component instance from its [`Spec`](crate::Spec) and
    /// returns the spec's typed port handles bound to the allocated
    /// [`ComponentId`] (sequential from 1).
    ///
    /// The emitted instance carries the spec's kind string, declared
    /// ports, and parameter map; the declared parameter set is checked
    /// at [`build`](Self::build) — required parameters must be present,
    /// and every supplied key declared with a value inside its kind and
    /// range.
    pub fn add<S: Spec>(&mut self, spec: S) -> S::Instance {
        let id = ComponentId(self.next_component);
        self.next_component += 1;
        // The spec's own `PortDecl::unit`/`ParamDecl::unit` declarations
        // stay spec-level — they constrain `connect`, `port_unit`, and
        // `param_unit` but do not emit into the document, whose units
        // record composition declarations only: an unchanged
        // composition emits identical bytes across a compatible
        // crossing, whatever the platform's spec tables gain.
        let port_decls = spec.ports();
        self.components.push(ComponentInstance {
            id,
            kind: spec.kind().to_string(),
            parameters: spec.parameter_values().clone(),
            rationalization: spec.rationalization().cloned(),
            ports: port_decls
                .iter()
                .map(|decl| {
                    (
                        decl.name.clone(),
                        Port {
                            direction: decl.direction,
                            value_type: decl.kind,
                            unit: None,
                        },
                    )
                })
                .collect(),
            parameter_units: BTreeMap::new(),
        });
        self.parameter_sets
            .push((id, spec.declared_parameters().map(<[_]>::to_vec)));
        self.port_sets.push((id, port_decls));
        spec.instance(id)
    }

    /// Wires `from` into `to`: `from` must produce a value — an `In`
    /// point, an `Out` port, or a `Source<Dynamic>` — and `to` must
    /// consume one, both carrying the same `T`.
    ///
    /// For statically typed ends this is a compile-time check; for
    /// [`Dynamic`](crate::Dynamic) ends — and for resolved ids and port
    /// names — the check lands at [`build`](Self::build) as a named
    /// [`ValidationError`]. Port handles are `Clone`, not `Copy`: reuse
    /// one across connections by passing `&port`.
    ///
    /// Where both ends already declare a `unit`
    /// ([`unit`](Self::unit), [`port_unit`](Self::port_unit), or a
    /// spec's [`PortDecl::unit`](crate::PortDecl::unit)) the declarations
    /// must agree — a connection carrying a value in one unit into a
    /// consumer engineered for another is a programming error and
    /// panics naming both ends. Declarations landing after the
    /// connection face the same check at [`build`](Self::build) as a
    /// named [`ValidationError`].
    ///
    /// # Panics
    ///
    /// Both ends resolve and declare disagreeing units.
    pub fn connect<T, F, K>(&mut self, from: F, to: K) -> &mut Self
    where
        F: Into<Source<T>>,
        K: Into<Sink<T>>,
    {
        let from_endpoint = from.into().endpoint().clone();
        let to_endpoint = to.into().endpoint().clone();
        if let (Some(Some(from_unit)), Some(Some(to_unit))) = (
            self.declared_unit(&from_endpoint),
            self.declared_unit(&to_endpoint),
        ) {
            assert!(
                from_unit == to_unit,
                "connect wires {from_endpoint:?} declaring {from_unit:?} to {to_endpoint:?} declaring {to_unit:?} — wired ends must declare equal units"
            );
        }
        self.connections.push(Connection {
            from: from_endpoint,
            to: to_endpoint,
        });
        self
    }

    /// Emits the composed [`PlantModel`].
    ///
    /// Checks the parts types cannot express, in order: each component's
    /// declared parameter set — required parameters present, supplied
    /// parameters declared, and each value inside its declared kind and
    /// range — then the document's own [`validate`](PlantModel::validate)
    /// so every id, channel reference, direction, and kind resolves
    /// exactly as a loaded document would. Returns the first failure;
    /// an `Ok` document loads, validates, and assembles identically to a
    /// hand-written one.
    pub fn build(mut self) -> Result<PlantModel, BuildError> {
        for component in &self.components {
            let Some(declared) = self
                .parameter_sets
                .iter()
                .find_map(|(id, decls)| (*id == component.id).then_some(decls))
                .and_then(|decls| decls.as_ref())
            else {
                continue;
            };
            for decl in declared {
                match component.parameters.get(decl.name) {
                    None if decl.required => {
                        return Err(BuildError::MissingParameter {
                            component: component.id,
                            parameter: decl.name.to_string(),
                        });
                    }
                    None => {}
                    Some(value) => {
                        if value.kind() != decl.kind {
                            return Err(BuildError::ParameterKindMismatch {
                                component: component.id,
                                parameter: decl.name.to_string(),
                                expected: decl.kind,
                                found: value.kind(),
                            });
                        }
                        if let Some(range) = decl.range
                            && !range.contains(*value)
                        {
                            return Err(BuildError::ParameterOutOfRange {
                                component: component.id,
                                parameter: decl.name.to_string(),
                                value: *value,
                            });
                        }
                    }
                }
            }
            for name in component.parameters.keys() {
                if !declared.iter().any(|decl| decl.name == name) {
                    return Err(BuildError::UnknownParameter {
                        component: component.id,
                        parameter: name.clone(),
                    });
                }
            }
        }

        // A signal that declares no `unit` inherits its source point's
        // declared unit: the composition declares the dimension once —
        // on the point — and the display metadata follows it, so the
        // name consumers render cannot silently drift from the unit the
        // value is wired in. An explicit signal declaration stays and
        // `validate` rejects it where it disagrees.
        for signal in &mut self.signals {
            if signal.unit.is_none()
                && let Some(unit) = self
                    .io_points
                    .iter()
                    .find(|point| point.id == signal.source)
                    .and_then(|point| point.unit.as_ref())
            {
                signal.unit = Some(unit.clone());
            }
        }

        let model = PlantModel {
            version: MODEL_VERSION,
            devices: self.devices,
            io_points: self.io_points,
            signals: self.signals,
            components: self.components,
            connections: self.connections,
            equipment: self.equipment,
            views: self.views,
        };
        let errors = model.validate();
        if errors.is_empty() {
            Ok(model)
        } else {
            Err(BuildError::Invalid(errors))
        }
    }
}

/// Builder-lite for a [`Signal`]'s optional display metadata, returned
/// by [`PlantBuilder::signal`].
pub struct SignalBuilder<'p> {
    signal: &'p mut Signal,
}

impl SignalBuilder<'_> {
    /// The engineering unit of the carried value, e.g. `"degC"` — where
    /// the source [`IoPoint`](dcs_model::IoPoint) declares a `unit` the
    /// signal inherits it at [`build`](PlantBuilder::build) and an
    /// explicit declaration here must agree with it; declare the
    /// dimension once — on the point through [`PlantBuilder::unit`] —
    /// unless the signal genuinely stands alone.
    pub fn unit(self, unit: &str) -> Self {
        self.signal.unit = Some(unit.to_string());
        self
    }

    /// The human-facing description of the signal.
    pub fn description(self, description: &str) -> Self {
        self.signal.description = Some(description.to_string());
        self
    }

    /// The display group the monitoring UI files the signal under.
    pub fn group(self, group: &str) -> Self {
        self.signal.group = Some(group.to_string());
        self
    }
}
