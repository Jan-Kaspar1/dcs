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
use crate::spec::{ParamDecl, Spec};
use dcs_core::{Direction, PointId, PointType, SignalId, Value, ValueKind};
use dcs_model::{
    Channel, ChannelRef, ComponentId, ComponentInstance, Connection, Device, DeviceId, IoPoint,
    MODEL_VERSION, PlantModel, Port, Signal, ValidationError,
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
    /// Each component's spec-declared parameter set, for `build`'s
    /// checks; `None` marks a spec leaving its parameters unchecked.
    parameter_sets: Vec<(ComponentId, Option<Vec<ParamDecl>>)>,
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
            parameter_sets: Vec::new(),
            next_device: 1,
            next_component: 1,
        }
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
        self.components.push(ComponentInstance {
            id,
            kind: spec.kind().to_string(),
            parameters: spec.parameter_values().clone(),
            rationalization: spec.rationalization().cloned(),
            ports: spec
                .ports()
                .into_iter()
                .map(|decl| {
                    (
                        decl.name,
                        Port {
                            direction: decl.direction,
                            value_type: decl.kind,
                        },
                    )
                })
                .collect(),
        });
        self.parameter_sets
            .push((id, spec.declared_parameters().map(<[_]>::to_vec)));
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
    pub fn connect<T, F, K>(&mut self, from: F, to: K) -> &mut Self
    where
        F: Into<Source<T>>,
        K: Into<Sink<T>>,
    {
        self.connections.push(Connection {
            from: from.into().endpoint().clone(),
            to: to.into().endpoint().clone(),
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
    pub fn build(self) -> Result<PlantModel, BuildError> {
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

        let model = PlantModel {
            version: MODEL_VERSION,
            devices: self.devices,
            io_points: self.io_points,
            signals: self.signals,
            components: self.components,
            connections: self.connections,
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
    /// The engineering unit of the carried value, e.g. `"degC"`.
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
