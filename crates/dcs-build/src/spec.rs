//! The [`Spec`] contract: a component kind's declared interface as data.
//!
//! A spec is the composition-time half of a registered component kind.
//! Where the kind's `describe()` descriptor tells the monitoring UI how
//! to draw an instance, the spec tells the
//! builder how to wire one: the model `kind` string, the named ports an
//! instance carries (name, [`Direction`], [`ValueKind`]), and the
//! parameter set — each [`ParamDecl`] declaring its name, kind, accepted
//! [`ParameterRange`], and whether the instance's map must supply it.
//!
//! The spec structs live in [`crate::specs`], declared beside the
//! descriptor vocabulary they mirror. The convention keeping the two in
//! sync: a spec's ports are listed in the kind's `io_requirements` order
//! and its parameters in `describe` order, and
//! `dcs-blocks/tests/spec_drift.rs` compares every spec against the
//! registered kind's `describe()` output and pins the spec table
//! against the registered-kind list — a kind whose spec and descriptor
//! disagree, or a registered kind with no spec, fails that test, so
//! both are updated together.

use crate::endpoint::{Dynamic, Sink, Source};
use dcs_core::{Direction, ParameterRange, Value, ValueKind};
use dcs_model::ComponentId;
use std::collections::BTreeMap;

/// A component instance's parameter map: the `parameters` field of the
/// emitted [`ComponentInstance`](dcs_model::ComponentInstance).
pub type Parameters = BTreeMap<String, Value>;

/// Builds a [`Parameters`] map from `(name, value)` pairs.
///
/// ```rust
/// let parameters = dcs_build::parameters([
///     ("kp", dcs_build::Value::Float(0.5)),
///     ("preset", dcs_build::Value::Int(4)),
/// ]);
/// assert_eq!(parameters.len(), 2);
/// ```
pub fn parameters<const N: usize>(pairs: [(&str, Value); N]) -> Parameters {
    pairs
        .into_iter()
        .map(|(name, value)| (name.to_string(), value))
        .collect()
}

/// One named port of a component kind: the declaration a spec carries.
///
/// Mirrors the kind's [`PortDescriptor`](dcs_core::PortDescriptor) minus
/// the monitoring-only role hint — name, direction, and value kind are
/// the composition contract.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PortDecl {
    /// The port's name within the component.
    pub name: String,
    /// `In` ports consume a connection's value; `Out` ports produce one.
    pub direction: Direction,
    /// The port's declared value kind.
    pub kind: ValueKind,
}

/// One declared parameter of a component kind.
///
/// Mirrors the kind's [`ParameterDescriptor`](dcs_core::ParameterDescriptor)
/// plus the `required` flag its `from_parameters` constructor implies:
/// [`PlantBuilder::build`](crate::PlantBuilder::build) reports a
/// [`MissingParameter`](crate::BuildError::MissingParameter) for a
/// required parameter the instance's map does not supply.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ParamDecl {
    /// The parameter's name in the model's parameter map.
    pub name: &'static str,
    /// The parameter's declared value kind.
    pub kind: ValueKind,
    /// The inclusive bound the kind reports for the parameter, when any.
    /// `build` rejects a supplied value outside it.
    pub range: Option<ParameterRange>,
    /// Whether the instance's parameter map must carry this key.
    pub required: bool,
}

/// A [`PortDecl`]: `port("pv", Direction::In, ValueKind::Float)`.
pub fn port(name: &str, direction: Direction, kind: ValueKind) -> PortDecl {
    PortDecl {
        name: name.to_string(),
        direction,
        kind,
    }
}

/// A required [`ParamDecl`]: `required("kp", ValueKind::Float,
/// Some(FINITE_F64))`.
pub const fn required(
    name: &'static str,
    kind: ValueKind,
    range: Option<ParameterRange>,
) -> ParamDecl {
    ParamDecl {
        name,
        kind,
        range,
        required: true,
    }
}

/// An optional [`ParamDecl`]: the instance's map may omit the key.
pub const fn optional(
    name: &'static str,
    kind: ValueKind,
    range: Option<ParameterRange>,
) -> ParamDecl {
    ParamDecl {
        name,
        kind,
        range,
        required: false,
    }
}

/// The inclusive bound a required-finite `Float` parameter accepts:
/// every finite double. Mirrors `dcs-blocks`' `describe::FINITE_F64`.
pub const FINITE_F64: ParameterRange = ParameterRange {
    min: Value::Float(-f64::MAX),
    max: Value::Float(f64::MAX),
};

/// The inclusive bound a strictly positive finite `Float` parameter
/// accepts. Mirrors `dcs-blocks`' `describe::POSITIVE_F64`.
pub const POSITIVE_F64: ParameterRange = ParameterRange {
    min: Value::Float(f64::MIN_POSITIVE),
    max: Value::Float(f64::MAX),
};

/// The inclusive bound a non-negative finite `Float` parameter accepts.
/// Mirrors `dcs-blocks`' `describe::NONNEGATIVE_F64`.
pub const NONNEGATIVE_F64: ParameterRange = ParameterRange {
    min: Value::Float(0.0),
    max: Value::Float(f64::MAX),
};

/// The inclusive bound a non-negative `Int` parameter accepts. Mirrors
/// `dcs-blocks`' `describe::NONNEGATIVE_INT`.
pub const NONNEGATIVE_INT: ParameterRange = ParameterRange {
    min: Value::Int(0),
    max: Value::Int(i64::MAX),
};

/// The inclusive bound a `Float` parameter in `(0, 1]` accepts — a
/// positive fraction such as a per-tick smoothing constant. The lower
/// bound is the smallest positive normal double — the tightest
/// inclusive bound on `x > 0` the type can express. Mirrors
/// `dcs-blocks`' `describe::FRACTION_F64`.
pub const FRACTION_F64: ParameterRange = ParameterRange {
    min: Value::Float(f64::MIN_POSITIVE),
    max: Value::Float(1.0),
};

/// A component kind's declared interface, supplied to
/// [`PlantBuilder::add`](crate::PlantBuilder::add).
///
/// Implementors are plain data: the kind string, the port list, the
/// declared parameter set, and the instance's parameter map. `Instance`
/// is the struct of typed port handles `add` returns — each field a
/// [`Source<T>`] for an `Out` port or a [`Sink<T>`] for an `In` port, so
/// connecting two ports checks direction and value kind at compile time.
pub trait Spec {
    /// The typed port handles [`add`](crate::PlantBuilder::add) returns.
    type Instance;

    /// The model `kind` string written into the emitted instance.
    fn kind(&self) -> &str;

    /// The kind's declared ports, in the order the kind's
    /// `io_requirements` reports them.
    fn ports(&self) -> Vec<PortDecl>;

    /// The kind's declared parameter set, or `None` when the set is not
    /// statically known — the instance's parameters then go unchecked.
    fn declared_parameters(&self) -> Option<&[ParamDecl]>;

    /// The instance's supplied parameter map.
    fn parameter_values(&self) -> &Parameters;

    /// The typed port handles bound to the allocated component `id`.
    fn instance(&self, id: ComponentId) -> Self::Instance;
}

/// A spec assembled from data, for component kinds without a dedicated
/// spec struct.
///
/// The instance's ports connect through [`Source<Dynamic>`] /
/// [`Sink<Dynamic>`] ends, so port names, directions, and kinds resolve
/// at [`build`](crate::PlantBuilder::build) rather than at compile time.
/// `declared_parameters` of `Some` enables the same parameter checks a
/// static spec gets; `None` leaves the instance's map unchecked.
#[derive(Debug, Clone)]
pub struct DynamicSpec {
    /// The model `kind` string.
    pub kind: String,
    /// The kind's declared ports.
    pub ports: Vec<PortDecl>,
    /// The declared parameter set; `None` skips parameter checks.
    pub declared_parameters: Option<Vec<ParamDecl>>,
    /// The instance's parameter map.
    pub parameters: Parameters,
}

impl DynamicSpec {
    /// A dynamic spec for `kind` declaring `ports`; parameters start
    /// empty and unchecked.
    pub fn new(kind: impl Into<String>, ports: Vec<PortDecl>) -> Self {
        Self {
            kind: kind.into(),
            ports,
            declared_parameters: None,
            parameters: Parameters::new(),
        }
    }
}

/// The untyped instance [`DynamicSpec`] produces: every port handle
/// resolves at [`build`](crate::PlantBuilder::build).
#[derive(Debug, Clone, Copy)]
pub struct DynamicInstance {
    id: ComponentId,
}

impl DynamicInstance {
    /// The allocated component id.
    pub fn id(&self) -> ComponentId {
        self.id
    }

    /// A producing end for port `name`: the port must exist and be an
    /// `Out` port, checked at [`build`](crate::PlantBuilder::build).
    pub fn source(&self, name: &str) -> Source<Dynamic> {
        Source::dynamic(dcs_model::Endpoint::Port(dcs_model::PortRef {
            component: self.id,
            name: name.to_string(),
        }))
    }

    /// A consuming end for port `name`: the port must exist and be an
    /// `In` port, checked at [`build`](crate::PlantBuilder::build).
    pub fn sink(&self, name: &str) -> Sink<Dynamic> {
        Sink::dynamic(dcs_model::Endpoint::Port(dcs_model::PortRef {
            component: self.id,
            name: name.to_string(),
        }))
    }
}

impl Spec for DynamicSpec {
    type Instance = DynamicInstance;

    fn kind(&self) -> &str {
        &self.kind
    }

    fn ports(&self) -> Vec<PortDecl> {
        self.ports.clone()
    }

    fn declared_parameters(&self) -> Option<&[ParamDecl]> {
        self.declared_parameters.as_deref()
    }

    fn parameter_values(&self) -> &Parameters {
        &self.parameters
    }

    fn instance(&self, id: ComponentId) -> Self::Instance {
        DynamicInstance { id }
    }
}
