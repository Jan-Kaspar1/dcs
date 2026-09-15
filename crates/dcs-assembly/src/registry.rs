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

    /// The bound members of the `prefix`-indexed port family — the ports
    /// named `prefix` immediately followed by a base-10 index (`"trip_"`
    /// binds `trip_1`, `trip_2`, …) — ordered by the numeric suffix,
    /// never lexically: a `_10` member sorts after `_2`, not between
    /// `_1` and `_2`.
    ///
    /// This is the convention for indexed port families: a kind whose
    /// declared I/O includes a variable-arity `prefix1` … `prefixN` set —
    /// `interlock`'s `trip_N`, `bool-gate`'s `in_N` — discovers the bound
    /// members through `indexed`, and a kind declaring several families
    /// in lockstep — `pump-group`'s `cmd_`/`run_`/`fault_`/`avail_` per
    /// pump — uses [`indexed_families`](Self::indexed_families).
    ///
    /// A bound port whose `prefix` suffix does not parse as an index is
    /// not a member and is ignored — it stays an ordinary port the kind
    /// may [`require`](Self::require) by name. Indices need not be
    /// contiguous: the members return in index order whatever the gaps —
    /// a kind requiring a contiguous `1..=N` family, where a gap or a
    /// missing member must fail, uses
    /// [`indexed_families`](Self::indexed_families), which reports the
    /// missing member as [`BuildError::UnboundPort`].
    pub fn indexed(&self, prefix: &str) -> Vec<PointId> {
        self.ports
            .iter()
            .filter_map(|(name, point)| {
                name.strip_prefix(prefix)
                    .and_then(|suffix| suffix.parse::<usize>().ok())
                    .map(|index| (index, *point))
            })
            .collect::<BTreeMap<_, _>>()
            .into_values()
            .collect()
    }

    /// The shared `1..=count` member sets of the named indexed families —
    /// the count-and-require-all discovery a multi-family kind follows:
    /// `count` is the highest index bound in any named family, and every
    /// index in `1..=count` must bind every family. A partial family or
    /// an index gap fails [`BuildError::UnboundPort`] naming the missing
    /// `prefix<index>` member — the same report [`require`](Self::require)
    /// produces — checked index-major in `prefixes` order, so the named
    /// member is the first missing one at the lowest index.
    ///
    /// The returned vec holds one entry per index in `1..=count`, each an
    /// array of the families' bound points in `prefixes` order: member
    /// `i` is `members[i - 1]`. An index-`0` member is never required —
    /// the range starts at `1` — and a bound port whose suffix does not
    /// parse as an index contributes nothing, exactly as
    /// [`indexed`](Self::indexed) treats it. A single contiguous family
    /// is the one-prefix case.
    pub fn indexed_families<const N: usize>(
        &self,
        prefixes: [&str; N],
    ) -> Result<Vec<[PointId; N]>, BuildError> {
        let count = prefixes
            .iter()
            .flat_map(|prefix| {
                self.ports.keys().filter_map(|name| {
                    name.strip_prefix(prefix)
                        .and_then(|suffix| suffix.parse::<usize>().ok())
                })
            })
            .max()
            .unwrap_or(0);
        let mut members = Vec::with_capacity(count);
        for index in 1..=count {
            let mut member = [PointId(0); N];
            for (slot, prefix) in member.iter_mut().zip(prefixes) {
                *slot = self.require(&format!("{prefix}{index}"))?;
            }
            members.push(member);
        }
        Ok(members)
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::LazyLock;

    static PARAMETERS: LazyLock<BTreeMap<String, Value>> = LazyLock::new(BTreeMap::new);
    static POINTS: LazyLock<PointMap> = LazyLock::new(PointMap::new);

    /// A spec standing in for one instance's resolved bindings; the
    /// indexed-family accessors read `ports` alone.
    fn spec<'m>(ports: &'m BTreeMap<String, PointId>) -> ComponentSpec<'m> {
        ComponentSpec {
            name: "test:1".to_string(),
            id: ComponentId(1),
            parameters: &PARAMETERS,
            ports,
            points: &POINTS,
        }
    }

    /// `name → PointId` pairs as the bound-port map.
    fn bound(entries: &[(&str, u64)]) -> BTreeMap<String, PointId> {
        entries
            .iter()
            .map(|(name, point)| (name.to_string(), PointId(*point)))
            .collect()
    }

    /// `indexed` orders the family by numeric suffix: across the
    /// two-digit boundary a `_10` member sorts after `_2`, where the
    /// port map's lexical order — and any lexical sort — would place it
    /// between `_1` and `_2`. Each member binds a point id equal to its
    /// index so the returned order asserts the suffix ordering directly.
    #[test]
    fn indexed_orders_members_by_numeric_suffix() {
        let ports: BTreeMap<String, PointId> = (1..=12u64)
            .map(|index| (format!("trip_{index}"), PointId(index)))
            .collect();
        assert_eq!(
            spec(&ports).indexed("trip_"),
            (1..=12).map(PointId).collect::<Vec<_>>()
        );
    }

    /// A bound port whose `prefix` suffix does not parse as an index is
    /// not a member — the preserved rule ignores it — and neither is a
    /// port merely sharing a leading substring. A gap in the bound
    /// indices does not fail or reorder: the bound members return in
    /// index order, matching the per-callsite collection the helper
    /// consolidates.
    #[test]
    fn indexed_ignores_non_members_and_returns_gapped_members_in_order() {
        let ports = bound(&[
            ("in_1", 1),
            ("in_3", 3),
            ("in_x", 90),
            ("in_", 91),
            ("input_2", 92),
            ("out", 93),
        ]);
        assert_eq!(
            spec(&ports).indexed("in_"),
            vec![PointId(1), PointId(3)]
        );
        // A family with no bound members is empty, not an error.
        assert_eq!(spec(&ports).indexed("trip_"), Vec::<PointId>::new());
    }

    /// `indexed_families` discovers the shared count as the highest
    /// index bound across the named families and returns each member's
    /// bound points in `prefixes` order.
    #[test]
    fn indexed_families_binds_every_member_in_prefix_order() {
        let ports = bound(&[
            ("cmd_1", 11),
            ("run_1", 21),
            ("fault_1", 31),
            ("avail_1", 41),
            ("cmd_2", 12),
            ("run_2", 22),
            ("fault_2", 32),
            ("avail_2", 42),
        ]);
        assert_eq!(
            spec(&ports)
                .indexed_families(["cmd_", "run_", "fault_", "avail_"])
                .unwrap(),
            vec![
                [PointId(11), PointId(21), PointId(31), PointId(41)],
                [PointId(12), PointId(22), PointId(32), PointId(42)],
            ]
        );
    }

    /// A partial family — one family short of the shared count — fails
    /// `UnboundPort` naming the missing `prefix<index>` member: `run_2`
    /// here, checked index-major so the lowest index's first missing
    /// family member is named.
    #[test]
    fn indexed_families_fails_unbound_port_naming_a_partial_familys_member() {
        let ports = bound(&[
            ("cmd_1", 11),
            ("run_1", 21),
            ("fault_1", 31),
            ("avail_1", 41),
            ("cmd_2", 12),
            ("fault_2", 32),
            ("avail_2", 42),
        ]);
        match spec(&ports).indexed_families(["cmd_", "run_", "fault_", "avail_"]) {
            Err(BuildError::UnboundPort { port }) => assert_eq!(port, "run_2"),
            other => panic!("expected UnboundPort, got {other:?}"),
        }
    }

    /// An index gap — an index below the highest bound one missing in a
    /// family — fails `UnboundPort` naming the gapped member: `fault_2`
    /// while `fault_3` binds.
    #[test]
    fn indexed_families_fails_unbound_port_naming_a_gap() {
        let ports = bound(&[
            ("cmd_1", 11),
            ("run_1", 21),
            ("fault_1", 31),
            ("avail_1", 41),
            ("cmd_2", 12),
            ("run_2", 22),
            ("avail_2", 42),
            ("cmd_3", 13),
            ("run_3", 23),
            ("fault_3", 33),
            ("avail_3", 43),
        ]);
        match spec(&ports).indexed_families(["cmd_", "run_", "fault_", "avail_"]) {
            Err(BuildError::UnboundPort { port }) => assert_eq!(port, "fault_2"),
            other => panic!("expected UnboundPort, got {other:?}"),
        }
    }

    /// Suffixes that do not parse as an index contribute no family
    /// membership, and an index-`0` member is never required — the
    /// shared range is `1..=count`.
    #[test]
    fn indexed_families_skips_non_numeric_and_zero_suffixes() {
        let ports = bound(&[
            ("cmd_x", 90),
            ("run_", 91),
            ("cmd_0", 92),
            ("run_0", 93),
            ("cmd_1", 11),
            ("run_1", 21),
        ]);
        assert_eq!(
            spec(&ports).indexed_families(["cmd_", "run_"]).unwrap(),
            vec![[PointId(11), PointId(21)]]
        );
        // No parseable member at any index ≥ 1: no members, no error.
        assert_eq!(
            spec(&bound(&[("cmd_x", 90)]))
                .indexed_families(["cmd_", "run_"])
                .unwrap(),
            Vec::<[PointId; 2]>::new()
        );
    }
}
