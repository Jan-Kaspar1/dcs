//! Typed connection endpoints: the handles `connect` checks statically.
//!
//! A [`Source<T>`] is a producing end — an `In` point or a component's
//! `Out` port — and a [`Sink<T>`] is a consuming end — an `Out` point or
//! a component's `In` port. `T` is the carried value's Rust type
//! (`bool`, `i64`, `f64`, via [`PointType`]), so
//! [`PlantBuilder::connect`](crate::PlantBuilder::connect) accepts only
//! a `Source<T>` feeding a `Sink<T>`: a wrong-direction or wrong-kind
//! connection fails to compile.
//!
//! Ends whose shape is only known from data — a port on a
//! [`DynamicSpec`](crate::DynamicSpec) instance, or an endpoint
//! assembled by hand — use the [`Dynamic`] marker. `Source<Dynamic>` and
//! `Sink<Dynamic>` still can only occupy their producing/consuming
//! positions, but their declared direction and value kind are checked
//! when [`PlantBuilder::build`](crate::PlantBuilder::build) validates the
//! emitted document instead of at compile time. A statically typed end
//! joins a dynamic connection through [`Source::erase`] /
//! [`Sink::erase`], which deliberately drops the static check.

use dcs_core::{PointId, PointType};
use dcs_model::{ComponentId, Endpoint, PortRef};
use std::fmt;
use std::marker::PhantomData;

/// The type marker on an endpoint built from data rather than a spec.
///
/// `Source<Dynamic>`/`Sink<Dynamic>` carry no static value kind, so
/// `connect` between them compiles and the check happens at
/// [`build`](crate::PlantBuilder::build): the emitted document's
/// validation names the offending endpoint.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct Dynamic;

/// A producing connection end: an `In` point or a component `Out` port.
///
/// Obtained from [`field_input`](crate::PlantBuilder::field_input),
/// [`internal_input`](crate::PlantBuilder::internal_input), a spec
/// instance's `Out` port field, or [`Source::dynamic`]. The type
/// parameter is the value's Rust type; `Source<Dynamic>` defers its
/// checks to [`build`](crate::PlantBuilder::build).
pub struct Source<T: ?Sized> {
    endpoint: Endpoint,
    marker: PhantomData<fn() -> T>,
}

/// A consuming connection end: an `Out` point or a component `In` port.
///
/// The mirror of [`Source`]: obtained from
/// [`field_output`](crate::PlantBuilder::field_output),
/// [`internal_output`](crate::PlantBuilder::internal_output), a spec
/// instance's `In` port field, or [`Sink::dynamic`].
pub struct Sink<T: ?Sized> {
    endpoint: Endpoint,
    marker: PhantomData<fn() -> T>,
}

/// A declared `In` point: a value the controller reads, producing into
/// connections.
///
/// `T` is the point's declared value type. `InPoint`s are `Copy` and can
/// feed any number of connections; [`Signal`](dcs_model::Signal) sources
/// accept them through `Into<PointId>`.
pub struct InPoint<T: ?Sized> {
    id: PointId,
    marker: PhantomData<fn() -> T>,
}

/// A declared `Out` point: a value the controller writes, consuming from
/// a connection.
pub struct OutPoint<T: ?Sized> {
    id: PointId,
    marker: PhantomData<fn() -> T>,
}

impl<T: ?Sized> Source<T> {
    /// The endpoint recorded into the emitted [`Connection`](dcs_model::Connection).
    pub fn endpoint(&self) -> &Endpoint {
        &self.endpoint
    }

    /// Drops the static type: the end keeps its endpoint but its checks
    /// move to [`build`](crate::PlantBuilder::build), for mixing with
    /// `Dynamic` ends.
    pub fn erase(self) -> Source<Dynamic> {
        Source {
            endpoint: self.endpoint,
            marker: PhantomData,
        }
    }
}

impl<T: PointType> Source<T> {
    /// A producing end naming port `name` on component `id`.
    pub(crate) fn port(component: ComponentId, name: &str) -> Self {
        Self {
            endpoint: Endpoint::Port(PortRef {
                component,
                name: name.to_string(),
            }),
            marker: PhantomData,
        }
    }
}

impl Source<Dynamic> {
    /// A producing end declared by data. The endpoint's declared
    /// direction must be producing — an `In` point or `Out` port — and
    /// its declared kind must equal the other end's, checked at
    /// [`build`](crate::PlantBuilder::build).
    pub fn dynamic(endpoint: Endpoint) -> Self {
        Self {
            endpoint,
            marker: PhantomData,
        }
    }
}

impl<T: ?Sized> Sink<T> {
    /// The endpoint recorded into the emitted [`Connection`](dcs_model::Connection).
    pub fn endpoint(&self) -> &Endpoint {
        &self.endpoint
    }

    /// Drops the static type: the end keeps its endpoint but its checks
    /// move to [`build`](crate::PlantBuilder::build), for mixing with
    /// `Dynamic` ends.
    pub fn erase(self) -> Sink<Dynamic> {
        Sink {
            endpoint: self.endpoint,
            marker: PhantomData,
        }
    }
}

impl<T: PointType> Sink<T> {
    /// A consuming end naming port `name` on component `id`.
    pub(crate) fn port(component: ComponentId, name: &str) -> Self {
        Self {
            endpoint: Endpoint::Port(PortRef {
                component,
                name: name.to_string(),
            }),
            marker: PhantomData,
        }
    }
}

impl Sink<Dynamic> {
    /// A consuming end declared by data. The endpoint's declared
    /// direction must be consuming — an `Out` point or `In` port — and
    /// its declared kind must equal the other end's, checked at
    /// [`build`](crate::PlantBuilder::build).
    pub fn dynamic(endpoint: Endpoint) -> Self {
        Self {
            endpoint,
            marker: PhantomData,
        }
    }
}

// `Source`/`Sink` clone despite the `String` inside `PortRef`; `Copy`
// handles (`InPoint`/`OutPoint`) clone by copy. The impls are manual so
// `T` carries no `Clone` bound.
impl<T: ?Sized> Clone for Source<T> {
    fn clone(&self) -> Self {
        Self {
            endpoint: self.endpoint.clone(),
            marker: PhantomData,
        }
    }
}

impl<T: ?Sized> Clone for Sink<T> {
    fn clone(&self) -> Self {
        Self {
            endpoint: self.endpoint.clone(),
            marker: PhantomData,
        }
    }
}

impl<T: ?Sized> fmt::Debug for Source<T> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Source")
            .field("endpoint", &self.endpoint)
            .finish()
    }
}

impl<T: ?Sized> fmt::Debug for Sink<T> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Sink")
            .field("endpoint", &self.endpoint)
            .finish()
    }
}

impl<T: ?Sized> From<&Source<T>> for Source<T> {
    fn from(source: &Source<T>) -> Self {
        source.clone()
    }
}

impl<T: ?Sized> From<&Sink<T>> for Sink<T> {
    fn from(sink: &Sink<T>) -> Self {
        sink.clone()
    }
}

impl<T: ?Sized> InPoint<T> {
    /// The declared point's id.
    pub fn id(&self) -> PointId {
        self.id
    }

    /// This point as a producing end with no static type — the checks
    /// move to [`build`](crate::PlantBuilder::build).
    pub fn erase(&self) -> Source<Dynamic> {
        Source::dynamic(Endpoint::Point(self.id))
    }
}

impl<T: PointType> InPoint<T> {
    /// A declared `In` point handle.
    pub(crate) fn new(id: PointId) -> Self {
        Self {
            id,
            marker: PhantomData,
        }
    }
}

impl<T: ?Sized> OutPoint<T> {
    /// The declared point's id.
    pub fn id(&self) -> PointId {
        self.id
    }

    /// This point as a consuming end with no static type — the checks
    /// move to [`build`](crate::PlantBuilder::build).
    pub fn erase(&self) -> Sink<Dynamic> {
        Sink::dynamic(Endpoint::Point(self.id))
    }
}

impl<T: PointType> OutPoint<T> {
    /// A declared `Out` point handle.
    pub(crate) fn new(id: PointId) -> Self {
        Self {
            id,
            marker: PhantomData,
        }
    }
}

impl<T: ?Sized> Clone for InPoint<T> {
    fn clone(&self) -> Self {
        *self
    }
}

impl<T: ?Sized> Copy for InPoint<T> {}

impl<T: ?Sized> Clone for OutPoint<T> {
    fn clone(&self) -> Self {
        *self
    }
}

impl<T: ?Sized> Copy for OutPoint<T> {}

impl<T: ?Sized> fmt::Debug for InPoint<T> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("InPoint").field("id", &self.id).finish()
    }
}

impl<T: ?Sized> fmt::Debug for OutPoint<T> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("OutPoint").field("id", &self.id).finish()
    }
}

impl<T: ?Sized> From<InPoint<T>> for Source<T> {
    fn from(point: InPoint<T>) -> Self {
        Self {
            endpoint: Endpoint::Point(point.id),
            marker: PhantomData,
        }
    }
}

impl<T: ?Sized> From<OutPoint<T>> for Sink<T> {
    fn from(point: OutPoint<T>) -> Self {
        Self {
            endpoint: Endpoint::Point(point.id),
            marker: PhantomData,
        }
    }
}

impl<T: ?Sized> From<InPoint<T>> for PointId {
    fn from(point: InPoint<T>) -> Self {
        point.id
    }
}

impl<T: ?Sized> From<OutPoint<T>> for PointId {
    fn from(point: OutPoint<T>) -> Self {
        point.id
    }
}
