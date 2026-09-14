//! The component-kind registry: model `kind` strings to constructors.

use crate::BuildError;
use dcs_core::{PointId, Value, ValueKind};
use dcs_model::ComponentId;
use dcs_runtime::{Component, PointMap};
use std::collections::BTreeMap;

/// What a registered constructor receives to build one model instance.
///
/// `parameters` is the instance's parameter map verbatim; `ports` resolves
/// each wired port to the logical point the model binds it to — an
/// `io_point`, or a synthesized internal point for port-to-port wiring.
/// Which port names a kind requires is the kind's contract, mirroring its
/// declared logical I/O. `points` is the resolved driver point map, so a
/// kind with type-parameterized variants can pick one from the bound
/// point's value kind.
pub struct ComponentSpec<'m> {
    /// The instance's diagnostic name — `"<kind>:<id>"`, e.g. `"pid:1"` —
    /// carried into executor diagnostics.
    pub name: String,
    /// The model instance's id.
    pub id: ComponentId,
    /// The instance's kind-specific parameters.
    pub parameters: &'m BTreeMap<String, Value>,
    /// The instance's bound ports: port name → bound logical point.
    pub ports: &'m BTreeMap<String, PointId>,
    /// The resolved point map: every served point's direction and kind.
    pub points: &'m PointMap,
}

impl ComponentSpec<'_> {
    /// The point bound to `port`, or [`BuildError::UnboundPort`] when the
    /// model wires no port of that name.
    pub fn require(&self, port: &str) -> Result<PointId, BuildError> {
        self.ports
            .get(port)
            .copied()
            .ok_or_else(|| BuildError::UnboundPort {
                port: port.to_string(),
            })
    }

    /// The point bound to `port`, if any — for ports a kind treats as
    /// optional.
    pub fn get(&self, port: &str) -> Option<PointId> {
        self.ports.get(port).copied()
    }

    /// The value kind the point map records for `point`, if it serves
    /// it — e.g. for choosing between a kind's `f64` and `i64` variants
    /// from the bound point's kind.
    pub fn point_kind(&self, point: PointId) -> Option<ValueKind> {
        self.points.get(point).map(|spec| spec.kind)
    }
}

/// A registered kind's constructor.
type Constructor = Box<dyn Fn(&ComponentSpec<'_>) -> Result<Box<dyn Component>, BuildError>>;

/// Maps plant-model component `kind` strings to constructors.
///
/// Registration is explicit: [`assemble`](crate::assemble) reports
/// [`AssemblyError::UnknownComponentKind`](crate::AssemblyError::UnknownComponentKind)
/// for an unregistered kind before any scan runs. `dcs-assembly` ships no
/// kinds of its own — the binary or test assembling a model registers the
/// component library it deploys (e.g. `dcs-blocks`), which keeps this crate
/// independent of any one component set.
#[derive(Default)]
pub struct ComponentRegistry {
    constructors: BTreeMap<String, Constructor>,
}

impl ComponentRegistry {
    /// An empty registry.
    pub fn new() -> Self {
        Self::default()
    }

    /// Registers `constructor` under `kind` and returns the registry, for
    /// chained registration.
    pub fn with<F>(mut self, kind: impl Into<String>, constructor: F) -> Self
    where
        F: Fn(&ComponentSpec<'_>) -> Result<Box<dyn Component>, BuildError> + 'static,
    {
        self.register(kind, constructor);
        self
    }

    /// Registers `constructor` under `kind`; a repeated kind replaces the
    /// earlier constructor.
    pub fn register<F>(&mut self, kind: impl Into<String>, constructor: F) -> &mut Self
    where
        F: Fn(&ComponentSpec<'_>) -> Result<Box<dyn Component>, BuildError> + 'static,
    {
        self.constructors.insert(kind.into(), Box::new(constructor));
        self
    }

    /// The registered kind strings, in sorted order — the set a
    /// coverage guard (e.g. `dcs-build`'s spec drift test) enumerates.
    pub fn kinds(&self) -> impl Iterator<Item = &str> {
        self.constructors.keys().map(String::as_str)
    }

    /// The constructor registered for `kind`.
    pub(crate) fn constructor(&self, kind: &str) -> Option<&Constructor> {
        self.constructors.get(kind)
    }
}
