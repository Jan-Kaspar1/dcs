//! Structural validation for [`PlantModel`] documents.
//!
//! [`PlantModel::load`](crate::PlantModel::load) runs these checks after
//! parsing; [`PlantModel::validate`] is also public so programmatically built
//! models can be checked before use. Validation reports every problem found
//! as a structured [`ValidationError`] naming the offending element rather
//! than panicking or stopping at the first failure.

use crate::model::{
    ComponentId, ComponentInstance, DeviceId, Direction, Endpoint, IoPoint, PlantModel,
};
use dcs_core::{PointId, SignalId, ValueKind};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::collections::hash_map::Entry;
use std::fmt;

/// A model collection whose element ids must be unique.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IdCollection {
    /// `devices`
    Device,
    /// `io_points`
    IoPoint,
    /// `signals`
    Signal,
    /// `components`
    Component,
}

impl fmt::Display for IdCollection {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            IdCollection::Device => "device",
            IdCollection::IoPoint => "io_point",
            IdCollection::Signal => "signal",
            IdCollection::Component => "component",
        })
    }
}

/// Which end of a [`Connection`](crate::Connection) an error refers to.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum End {
    /// The producing (`from`) end.
    From,
    /// The consuming (`to`) end.
    To,
}

impl fmt::Display for End {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            End::From => "from",
            End::To => "to",
        })
    }
}

/// One structural problem found by [`PlantModel::validate`], identifying the
/// offending element by id, name, or connection index.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum ValidationError {
    /// Two elements of one collection share an id.
    DuplicateId {
        /// The collection containing the duplicate.
        collection: IdCollection,
        /// The duplicated id.
        id: u64,
    },
    /// A point binds a channel on a device the model does not declare.
    UnknownDevice {
        /// The point with the dangling binding.
        point: PointId,
        /// The device it names.
        device: DeviceId,
    },
    /// A point binds a channel its device does not have.
    UnknownChannel {
        /// The point with the dangling binding.
        point: PointId,
        /// The device it names.
        device: DeviceId,
        /// The missing channel name.
        channel: String,
    },
    /// A point's direction disagrees with its bound channel's direction.
    ChannelDirectionMismatch {
        /// The offending point.
        point: PointId,
        /// The bound channel's device.
        device: DeviceId,
        /// The bound channel's name.
        channel: String,
        /// The point's declared direction.
        point_direction: Direction,
        /// The channel's declared direction.
        channel_direction: Direction,
    },
    /// A channel-bound point declares an `initial` value; the field owns a
    /// bound point's value, so the declaration has nothing to attach to.
    FieldInitial {
        /// The offending point.
        point: PointId,
    },
    /// An internal point — declared without a channel — carries no
    /// `initial` value; the scan image would hold nothing until a first
    /// write.
    MissingInitial {
        /// The offending point.
        point: PointId,
    },
    /// An internal point's `initial` value is a different kind than its
    /// declared `value_type`.
    InitialKindMismatch {
        /// The offending point.
        point: PointId,
        /// The point's declared value type.
        declared: ValueKind,
        /// The kind `initial` actually carries.
        initial: ValueKind,
    },
    /// A point declares `writable` but has direction `Out`. The command
    /// path refuses `Out`-point writes outright — operator influence on
    /// an output is engineered through components — so the flag can only
    /// mark an `In` point.
    WritableOut {
        /// The offending point.
        point: PointId,
    },
    /// A point declares a `stale_after_ticks` freshness budget but has
    /// direction `Out`. The budget lands on the input image during the
    /// read phase, which only `In` points take part in — an `Out`
    /// point's declaration has nothing to attach to.
    StaleOut {
        /// The offending point.
        point: PointId,
    },
    /// A point declares a `stale_after_ticks` freshness budget but binds
    /// no channel. The budget measures the lag of a driver-returned
    /// sample's tick, and an internal point is never driver-read — it
    /// can never go stale, so the declaration is meaningless there.
    StaleInternal {
        /// The offending point.
        point: PointId,
    },
    /// A point's value type disagrees with its bound channel's value type.
    ChannelTypeMismatch {
        /// The offending point.
        point: PointId,
        /// The bound channel's device.
        device: DeviceId,
        /// The bound channel's name.
        channel: String,
        /// The point's declared value type.
        point_type: ValueKind,
        /// The channel's declared value type.
        channel_type: ValueKind,
    },
    /// A signal names a source point the model does not declare.
    UnknownSource {
        /// The signal with the dangling reference.
        signal: SignalId,
        /// The point it names.
        point: PointId,
    },
    /// A connection endpoint names a point the model does not declare.
    UnknownPoint {
        /// Index of the offending connection in `connections`.
        connection: usize,
        /// Which end is dangling.
        end: End,
        /// The point it names.
        point: PointId,
    },
    /// A connection endpoint names a component the model does not declare.
    UnknownComponent {
        /// Index of the offending connection in `connections`.
        connection: usize,
        /// Which end is dangling.
        end: End,
        /// The component it names.
        component: ComponentId,
    },
    /// A connection endpoint names a port its component does not declare.
    UnknownPort {
        /// Index of the offending connection in `connections`.
        connection: usize,
        /// Which end is dangling.
        end: End,
        /// The component it names.
        component: ComponentId,
        /// The missing port name.
        port: String,
    },
    /// A connection's `from` end cannot produce a value or its `to` end
    /// cannot consume one.
    ConnectionDirectionMismatch {
        /// Index of the offending connection in `connections`.
        connection: usize,
        /// The end whose direction is wrong: `from` requires an `In` point
        /// or an `Out` port, `to` an `Out` point or an `In` port.
        end: End,
        /// The offending endpoint.
        endpoint: Endpoint,
        /// The direction the element declares.
        direction: Direction,
    },
    /// A connection's two ends carry different value types.
    ConnectionTypeMismatch {
        /// Index of the offending connection in `connections`.
        connection: usize,
        /// The `from` end's value type.
        from: ValueKind,
        /// The `to` end's value type.
        to: ValueKind,
    },
}

impl fmt::Display for ValidationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicateId { collection, id } => {
                write!(f, "duplicate {collection} id {id}")
            }
            Self::UnknownDevice { point, device } => write!(
                f,
                "io point {} binds a channel on unknown device {}",
                point.0, device.0
            ),
            Self::UnknownChannel {
                point,
                device,
                channel,
            } => write!(
                f,
                "io point {} binds unknown channel {channel:?} on device {}",
                point.0, device.0
            ),
            Self::ChannelDirectionMismatch {
                point,
                device,
                channel,
                point_direction,
                channel_direction,
            } => write!(
                f,
                "io point {} has direction {point_direction} but channel {channel:?} on device {} has direction {channel_direction}",
                point.0, device.0
            ),
            Self::FieldInitial { point } => write!(
                f,
                "io point {} declares an initial value but is bound to a channel",
                point.0
            ),
            Self::MissingInitial { point } => {
                write!(f, "internal io point {} declares no initial value", point.0)
            }
            Self::InitialKindMismatch {
                point,
                declared,
                initial,
            } => write!(
                f,
                "internal io point {} declares type {declared:?} but its initial value is {initial:?}",
                point.0
            ),
            Self::WritableOut { point } => write!(
                f,
                "io point {} declares writable but has direction out",
                point.0
            ),
            Self::StaleOut { point } => write!(
                f,
                "io point {} declares stale_after_ticks but has direction out",
                point.0
            ),
            Self::StaleInternal { point } => write!(
                f,
                "io point {} declares stale_after_ticks but binds no channel",
                point.0
            ),
            Self::ChannelTypeMismatch {
                point,
                device,
                channel,
                point_type,
                channel_type,
            } => write!(
                f,
                "io point {} has type {point_type:?} but channel {channel:?} on device {} has type {channel_type:?}",
                point.0, device.0
            ),
            Self::UnknownSource { signal, point } => write!(
                f,
                "signal {} sources unknown io point {}",
                signal.0, point.0
            ),
            Self::UnknownPoint {
                connection,
                end,
                point,
            } => write!(
                f,
                "connection {connection} {end} end names unknown io point {}",
                point.0
            ),
            Self::UnknownComponent {
                connection,
                end,
                component,
            } => write!(
                f,
                "connection {connection} {end} end names unknown component {}",
                component.0
            ),
            Self::UnknownPort {
                connection,
                end,
                component,
                port,
            } => write!(
                f,
                "connection {connection} {end} end names unknown port {port:?} on component {}",
                component.0
            ),
            Self::ConnectionDirectionMismatch {
                connection,
                end,
                endpoint,
                direction,
            } => write!(
                f,
                "connection {connection} {end} end {endpoint:?} has direction {direction} and cannot {} a value",
                match end {
                    End::From => "produce",
                    End::To => "consume",
                }
            ),
            Self::ConnectionTypeMismatch {
                connection,
                from,
                to,
            } => {
                write!(
                    f,
                    "connection {connection} carries {from:?} into a {to:?} end"
                )
            }
        }
    }
}

impl std::error::Error for ValidationError {}

/// Builds an id index for one collection, reporting each repeated id as
/// [`ValidationError::DuplicateId`]. The first element with a given id wins,
/// so later reference checks still resolve it.
fn index_by_id<'m, T>(
    collection: IdCollection,
    items: &'m [T],
    id: impl Fn(&T) -> u64,
    errors: &mut Vec<ValidationError>,
) -> HashMap<u64, &'m T> {
    let mut index = HashMap::with_capacity(items.len());
    for item in items {
        match index.entry(id(item)) {
            Entry::Occupied(_) => errors.push(ValidationError::DuplicateId {
                collection,
                id: id(item),
            }),
            Entry::Vacant(slot) => {
                slot.insert(item);
            }
        }
    }
    index
}

/// Resolves a connection endpoint to `(direction, value type, produces)`,
/// where "produces" means the element feeds values into the connection:
/// `In` points and `Out` ports produce, `Out` points and `In` ports consume.
/// Pushes the matching `Unknown*` error and returns `None` when the endpoint
/// does not resolve.
fn resolve_endpoint(
    endpoint: &Endpoint,
    connection: usize,
    end: End,
    points: &HashMap<u64, &IoPoint>,
    components: &HashMap<u64, &ComponentInstance>,
    errors: &mut Vec<ValidationError>,
) -> Option<(Direction, ValueKind, bool)> {
    match endpoint {
        Endpoint::Point(id) => {
            let Some(point) = points.get(&id.0) else {
                errors.push(ValidationError::UnknownPoint {
                    connection,
                    end,
                    point: *id,
                });
                return None;
            };
            Some((
                point.direction,
                point.value_type,
                point.direction == Direction::In,
            ))
        }
        Endpoint::Port(port_ref) => {
            let Some(component) = components.get(&port_ref.component.0) else {
                errors.push(ValidationError::UnknownComponent {
                    connection,
                    end,
                    component: port_ref.component,
                });
                return None;
            };
            let Some(port) = component.ports.get(&port_ref.name) else {
                errors.push(ValidationError::UnknownPort {
                    connection,
                    end,
                    component: port_ref.component,
                    port: port_ref.name.clone(),
                });
                return None;
            };
            Some((
                port.direction,
                port.value_type,
                port.direction == Direction::Out,
            ))
        }
    }
}

impl PlantModel {
    /// Runs the structural checks the schema cannot express in types:
    ///
    /// - every device, point, signal, and component id is unique;
    /// - every reference resolves: a bound point's channel names a real
    ///   device and channel, a signal's source names a real point, and each
    ///   connection endpoint names a real point, component, and port;
    /// - a bound point's direction and value type agree with its channel,
    ///   while an internal point — one declared without a channel — must
    ///   carry an `initial` value of its declared `value_type`, and a
    ///   channel-bound point must not declare one;
    /// - `writable` marks only `In` points: the command path refuses
    ///   `Out`-point writes outright, so an `Out` point carrying the flag
    ///   is reported, whether the point is channel-bound or internal;
    /// - `stale_after_ticks` marks only field `In` points: the freshness
    ///   budget applies where a driver read happens, so an `Out` point or
    ///   a channel-less internal point carrying it is reported;
    /// - each connection's `from` end produces a value (an `In` point or an
    ///   `Out` port) and its `to` end consumes one (an `Out` point or an `In`
    ///   port), with matching value types on both ends — internal points
    ///   follow the same direction and type rules as bound points.
    ///
    /// Returns every error found; an empty vector means the model is valid.
    pub fn validate(&self) -> Vec<ValidationError> {
        let mut errors = Vec::new();

        let devices = index_by_id(
            IdCollection::Device,
            &self.devices,
            |device| device.id.0,
            &mut errors,
        );
        let points = index_by_id(
            IdCollection::IoPoint,
            &self.io_points,
            |point| point.id.0,
            &mut errors,
        );
        let _signals = index_by_id(
            IdCollection::Signal,
            &self.signals,
            |signal| signal.id.0,
            &mut errors,
        );
        let components = index_by_id(
            IdCollection::Component,
            &self.components,
            |component| component.id.0,
            &mut errors,
        );

        for point in &self.io_points {
            // `writable` interacts with the direction rule: only `In`
            // points can be command targets, so the flag on an `Out`
            // point — field or internal — is a declaration error.
            if point.writable && point.direction == Direction::Out {
                errors.push(ValidationError::WritableOut { point: point.id });
            }
            // `stale_after_ticks` interacts with both the direction and
            // the channel rules: the freshness check runs on driver
            // reads in the input phase, so only a field `In` point can
            // carry the budget — an `Out` point or a channel-less
            // internal point declaring it is a declaration error.
            if point.stale_after_ticks.is_some() {
                if point.direction == Direction::Out {
                    errors.push(ValidationError::StaleOut { point: point.id });
                }
                if point.channel.is_none() {
                    errors.push(ValidationError::StaleInternal { point: point.id });
                }
            }
            let Some(reference) = &point.channel else {
                // An internal point's initial value is its whole declared
                // state: it must exist and match the declared value type.
                match point.initial {
                    None => errors.push(ValidationError::MissingInitial { point: point.id }),
                    Some(initial) if initial.kind() != point.value_type => {
                        errors.push(ValidationError::InitialKindMismatch {
                            point: point.id,
                            declared: point.value_type,
                            initial: initial.kind(),
                        });
                    }
                    Some(_) => {}
                }
                continue;
            };
            if point.initial.is_some() {
                errors.push(ValidationError::FieldInitial { point: point.id });
            }
            let Some(device) = devices.get(&reference.device.0) else {
                errors.push(ValidationError::UnknownDevice {
                    point: point.id,
                    device: reference.device,
                });
                continue;
            };
            let Some(channel) = device.channels.get(&reference.name) else {
                errors.push(ValidationError::UnknownChannel {
                    point: point.id,
                    device: reference.device,
                    channel: reference.name.clone(),
                });
                continue;
            };
            if point.direction != channel.direction {
                errors.push(ValidationError::ChannelDirectionMismatch {
                    point: point.id,
                    device: reference.device,
                    channel: reference.name.clone(),
                    point_direction: point.direction,
                    channel_direction: channel.direction,
                });
            }
            if point.value_type != channel.value_type {
                errors.push(ValidationError::ChannelTypeMismatch {
                    point: point.id,
                    device: reference.device,
                    channel: reference.name.clone(),
                    point_type: point.value_type,
                    channel_type: channel.value_type,
                });
            }
        }

        for signal in &self.signals {
            if !points.contains_key(&signal.source.0) {
                errors.push(ValidationError::UnknownSource {
                    signal: signal.id,
                    point: signal.source,
                });
            }
        }

        for (index, connection) in self.connections.iter().enumerate() {
            let from = resolve_endpoint(
                &connection.from,
                index,
                End::From,
                &points,
                &components,
                &mut errors,
            );
            let to = resolve_endpoint(
                &connection.to,
                index,
                End::To,
                &points,
                &components,
                &mut errors,
            );

            for (end, resolved, endpoint) in [
                (End::From, from, &connection.from),
                (End::To, to, &connection.to),
            ] {
                if let Some((direction, _, produces)) = resolved {
                    let compatible = match end {
                        End::From => produces,
                        End::To => !produces,
                    };
                    if !compatible {
                        errors.push(ValidationError::ConnectionDirectionMismatch {
                            connection: index,
                            end,
                            endpoint: endpoint.clone(),
                            direction,
                        });
                    }
                }
            }

            if let (Some((_, from_type, _)), Some((_, to_type, _))) = (from, to)
                && from_type != to_type
            {
                errors.push(ValidationError::ConnectionTypeMismatch {
                    connection: index,
                    from: from_type,
                    to: to_type,
                });
            }
        }

        errors
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{LoadError, PortRef};
    use dcs_core::Value;

    const MINIMAL: &str = include_str!("../fixtures/minimal.json");

    fn minimal() -> PlantModel {
        serde_json::from_str(MINIMAL).unwrap()
    }

    fn load_errors(source: &str) -> Vec<ValidationError> {
        match PlantModel::load(source) {
            Err(LoadError::Invalid(errors)) => errors,
            other => panic!("expected validation errors, got {other:?}"),
        }
    }

    #[test]
    fn minimal_fixture_is_valid() {
        assert!(minimal().validate().is_empty());
    }

    #[test]
    fn duplicate_id_is_rejected() {
        let errors = load_errors(include_str!("../fixtures/invalid/duplicate_id.json"));
        assert!(
            errors.contains(&ValidationError::DuplicateId {
                collection: IdCollection::IoPoint,
                id: 10,
            }),
            "{errors:?}"
        );
    }

    #[test]
    fn dangling_reference_is_rejected() {
        let errors = load_errors(include_str!("../fixtures/invalid/dangling_reference.json"));
        assert!(
            errors.contains(&ValidationError::UnknownPoint {
                connection: 0,
                end: End::From,
                point: PointId(99),
            }),
            "{errors:?}"
        );
    }

    #[test]
    fn direction_mismatch_is_rejected() {
        let errors = load_errors(include_str!("../fixtures/invalid/direction_mismatch.json"));
        assert!(
            errors.iter().any(|error| matches!(
                error,
                ValidationError::ConnectionDirectionMismatch {
                    connection: 0,
                    end: End::From,
                    ..
                }
            )),
            "{errors:?}"
        );
    }

    #[test]
    fn unknown_channel_is_rejected() {
        let errors = load_errors(include_str!("../fixtures/invalid/unknown_channel.json"));
        assert!(
            errors.contains(&ValidationError::UnknownChannel {
                point: PointId(10),
                device: DeviceId(1),
                channel: "ch9".to_string(),
            }),
            "{errors:?}"
        );
    }

    #[test]
    fn duplicate_ids_in_each_collection_are_rejected() {
        let mut model = minimal();
        model.devices.push(model.devices[0].clone());
        model.components.push(model.components[0].clone());
        model.signals.push(model.signals[0].clone());
        let errors = model.validate();
        for (collection, id) in [
            (IdCollection::Device, 1),
            (IdCollection::Component, 1),
            (IdCollection::Signal, 100),
        ] {
            assert!(
                errors.contains(&ValidationError::DuplicateId { collection, id }),
                "{errors:?}"
            );
        }
    }

    #[test]
    fn point_binding_to_unknown_device_is_rejected() {
        let mut model = minimal();
        model.io_points[0].channel.as_mut().unwrap().device = DeviceId(99);
        assert!(model.validate().contains(&ValidationError::UnknownDevice {
            point: PointId(10),
            device: DeviceId(99),
        }));
    }

    #[test]
    fn signal_source_must_resolve() {
        let mut model = minimal();
        model.signals[0].source = PointId(99);
        assert!(model.validate().contains(&ValidationError::UnknownSource {
            signal: SignalId(100),
            point: PointId(99),
        }));
    }

    #[test]
    fn signal_sourcing_unknown_point_is_rejected() {
        let errors = load_errors(include_str!("../fixtures/invalid/dangling_signal.json"));
        assert!(
            errors.contains(&ValidationError::UnknownSource {
                signal: SignalId(100),
                point: PointId(99),
            }),
            "{errors:?}"
        );
    }

    #[test]
    fn connection_to_unknown_component_and_port_is_rejected() {
        let mut model = minimal();
        model.connections[0].to = Endpoint::Port(PortRef {
            component: ComponentId(99),
            name: "in".to_string(),
        });
        assert!(
            model
                .validate()
                .contains(&ValidationError::UnknownComponent {
                    connection: 0,
                    end: End::To,
                    component: ComponentId(99),
                })
        );

        let mut model = minimal();
        model.connections[0].to = Endpoint::Port(PortRef {
            component: ComponentId(1),
            name: "nope".to_string(),
        });
        assert!(model.validate().contains(&ValidationError::UnknownPort {
            connection: 0,
            end: End::To,
            component: ComponentId(1),
            port: "nope".to_string(),
        }));
    }

    #[test]
    fn consuming_connection_end_must_not_produce() {
        // An `out` port produces a value; it cannot be a connection's `to`
        // end.
        let mut model = minimal();
        model.connections[0].to = Endpoint::Port(PortRef {
            component: ComponentId(1),
            name: "out".to_string(),
        });
        assert!(model.validate().iter().any(|error| matches!(
            error,
            ValidationError::ConnectionDirectionMismatch {
                connection: 0,
                end: End::To,
                ..
            }
        )));
    }

    #[test]
    fn connection_types_must_match() {
        let mut model = minimal();
        model.components[0].ports.get_mut("in").unwrap().value_type = ValueKind::Int;
        assert!(
            model
                .validate()
                .contains(&ValidationError::ConnectionTypeMismatch {
                    connection: 0,
                    from: ValueKind::Float,
                    to: ValueKind::Int,
                })
        );
    }

    #[test]
    fn point_must_agree_with_bound_channel() {
        let mut model = minimal();
        model.io_points[0].direction = Direction::Out;
        assert!(model.validate().iter().any(|error| matches!(
            error,
            ValidationError::ChannelDirectionMismatch {
                point: PointId(10),
                ..
            }
        )));

        let mut model = minimal();
        model.io_points[0].value_type = ValueKind::Int;
        assert!(model.validate().iter().any(|error| matches!(
            error,
            ValidationError::ChannelTypeMismatch {
                point: PointId(10),
                ..
            }
        )));
    }

    /// Turns point `index` of the minimal fixture into an internal point.
    fn make_internal(model: &mut PlantModel, index: usize, initial: Option<Value>) {
        model.io_points[index].channel = None;
        model.io_points[index].initial = initial;
    }

    #[test]
    fn internal_point_with_initial_is_valid() {
        let mut model = minimal();
        make_internal(&mut model, 0, Some(Value::Float(2.0)));
        assert!(model.io_points[0].is_internal());
        assert!(model.validate().is_empty());
        // The internal point keeps producing values as a connection's
        // `from` end — internal points follow the field-point rules.
        let json = serde_json::to_string(&model).unwrap();
        let reloaded = PlantModel::load(&json).unwrap();
        assert!(reloaded.io_points[0].is_internal());
        assert_eq!(reloaded, model);
    }

    #[test]
    fn internal_point_without_initial_is_rejected() {
        let mut model = minimal();
        make_internal(&mut model, 0, None);
        assert!(
            model
                .validate()
                .contains(&ValidationError::MissingInitial { point: PointId(10) })
        );
    }

    #[test]
    fn internal_point_initial_must_match_value_type() {
        let mut model = minimal();
        make_internal(&mut model, 0, Some(Value::Int(2)));
        assert!(
            model
                .validate()
                .contains(&ValidationError::InitialKindMismatch {
                    point: PointId(10),
                    declared: ValueKind::Float,
                    initial: ValueKind::Int,
                })
        );
    }

    #[test]
    fn bound_point_with_initial_is_rejected() {
        let mut model = minimal();
        model.io_points[0].initial = Some(Value::Float(0.0));
        assert!(
            model
                .validate()
                .contains(&ValidationError::FieldInitial { point: PointId(10) })
        );
    }

    #[test]
    fn writable_marks_only_in_points() {
        // A writable `In` point — bound or internal — is valid: it is the
        // model-declared command surface.
        let mut model = minimal();
        model.io_points[0].writable = true;
        assert!(model.validate().is_empty());
        make_internal(&mut model, 0, Some(Value::Float(25.0)));
        assert!(model.validate().is_empty());

        // `writable` on a bound `Out` point declares a command surface the
        // contract cannot honor.
        let mut model = minimal();
        model.io_points[1].writable = true;
        assert!(
            model
                .validate()
                .contains(&ValidationError::WritableOut { point: PointId(11) })
        );

        // The same rule applies to internal `Out` points.
        let mut model = minimal();
        make_internal(&mut model, 1, Some(Value::Float(0.0)));
        model.io_points[1].writable = true;
        assert!(
            model
                .validate()
                .contains(&ValidationError::WritableOut { point: PointId(11) })
        );
    }

    #[test]
    fn stale_after_ticks_marks_only_field_in_points() {
        // A field `In` point — the point a driver read serves — is the
        // only declaration the budget is valid on.
        let mut model = minimal();
        model.io_points[0].stale_after_ticks = Some(2);
        assert!(model.validate().is_empty());

        // On an `Out` point the budget has no read phase to apply to.
        let mut model = minimal();
        model.io_points[1].stale_after_ticks = Some(2);
        assert!(
            model
                .validate()
                .contains(&ValidationError::StaleOut { point: PointId(11) })
        );

        // On a channel-less internal point the driver is never read, so
        // the budget can never apply — rejected as meaningless.
        let mut model = minimal();
        make_internal(&mut model, 0, Some(Value::Float(2.0)));
        model.io_points[0].stale_after_ticks = Some(2);
        assert!(
            model
                .validate()
                .contains(&ValidationError::StaleInternal { point: PointId(10) })
        );

        // An internal `Out` point violates both rules.
        let mut model = minimal();
        make_internal(&mut model, 1, Some(Value::Float(0.0)));
        model.io_points[1].stale_after_ticks = Some(2);
        let errors = model.validate();
        assert!(errors.contains(&ValidationError::StaleOut { point: PointId(11) }));
        assert!(errors.contains(&ValidationError::StaleInternal { point: PointId(11) }));
    }
}
