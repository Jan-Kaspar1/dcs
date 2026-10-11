//! Code-engineered plant drawings referencing the authoritative model.

use crate::{ComponentId, PlantModel, ValidationError};
use dcs_core::{PointId, ValueKind};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};

/// A named process area drawn on a fixed 1200 by 760 coordinate plane.
/// Geometry changes presentation only; bindings retain normal control authority.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlantView {
    /// Unique view identity used for area navigation.
    pub id: String,
    /// Operator-facing area name.
    pub label: String,
    /// Parent area identity, or `None` for a root area.
    #[serde(default)]
    pub parent: Option<String>,
    /// Symbols in drawing order.
    pub nodes: Vec<PlantViewNode>,
    /// Presentation pipework, independent of control wiring.
    pub pipes: Vec<PlantViewPipe>,
}

impl PlantView {
    /// Creates an empty root area; set `parent` to nest it beneath another.
    pub fn new(id: impl Into<String>, label: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            label: label.into(),
            parent: None,
            nodes: Vec::new(),
            pipes: Vec::new(),
        }
    }
}

/// Explicit measurement display context, independent of control and alarm limits.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MeasurementDisplay {
    /// Finite lower display bound.
    pub min: f64,
    /// Finite upper display bound, strictly greater than min.
    pub max: f64,
    /// Optional finite normal band contained within the display range.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub normal: Option<[f64; 2]>,
}

/// One library symbol with an optional live model binding.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlantViewNode {
    /// Unique symbol identity within its view.
    pub id: String,
    /// Standard library glyph and fixed dimensions.
    pub symbol: PlantViewSymbol,
    /// Operator-facing symbol label.
    pub label: String,
    /// Left coordinate on the view's fixed plane.
    pub x: u32,
    /// Top coordinate on the view's fixed plane.
    pub y: u32,
    /// Existing model entity supplying live state and controls, or drawing only.
    #[serde(default)]
    pub binding: Option<PlantViewBinding>,
    /// Optional declared gauge context for a numeric point-bound measurement.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub display: Option<MeasurementDisplay>,
}

impl PlantViewNode {
    /// Places an unbound symbol; assign `binding` to expose an existing entity.
    pub fn new(
        id: impl Into<String>,
        symbol: PlantViewSymbol,
        label: impl Into<String>,
        x: u32,
        y: u32,
    ) -> Self {
        Self {
            id: id.into(),
            symbol,
            label: label.into(),
            x,
            y,
            binding: None,
            display: None,
        }
    }
}

/// Supported library glyphs; sizing belongs to the platform, not plant code.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PlantViewSymbol {
    /// Pump, 160 by 140.
    Pump,
    /// Motor, 160 by 140.
    Motor,
    /// Valve, 160 by 140.
    Valve,
    /// Vessel outline, 150 by 195, without inferred level.
    Tank,
    /// Numeric measurement, 190 by 100.
    Measurement,
    /// Static process annotation, 300 by 50.
    Label,
}

impl PlantViewSymbol {
    /// Fixed width and height on the view coordinate plane.
    pub const fn dimensions(self) -> (u32, u32) {
        match self {
            Self::Pump | Self::Motor | Self::Valve => (160, 140),
            Self::Tank => (150, 195),
            Self::Measurement => (190, 100),
            Self::Label => (300, 50),
        }
    }
}

/// Typed references to existing entities; a binding grants no new command rights.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PlantViewBinding {
    /// Complete reusable equipment by its stable identity.
    Equipment(String),
    /// Primitive component by its model identity.
    Component(ComponentId),
    /// Numeric logical point, used by measurement symbols.
    Point(PointId),
}

/// A drawn pipe between two symbols in the same view.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlantViewPipe {
    /// Start of the drawn pipe.
    pub from: PlantViewPipeEnd,
    /// End of the drawn pipe.
    pub to: PlantViewPipeEnd,
}

/// A symbol and attachment side within a drawn pipe's own view.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlantViewPipeEnd {
    /// Symbol identity within the view.
    pub node: String,
    /// Attachment side.
    pub port: PlantViewPort,
}

/// Standard pipe attachment sides.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PlantViewPort {
    /// Top.
    N,
    /// Right.
    E,
    /// Bottom.
    S,
    /// Left.
    W,
}

pub(crate) fn validate(model: &PlantModel, errors: &mut Vec<ValidationError>) {
    let invalid = |view: &str, reason: String| ValidationError::InvalidPlantView {
        view: view.to_string(),
        reason,
    };
    if model.views.len() > 64 {
        errors.push(invalid("", "at most 64 views are supported".to_string()));
    }
    let views: HashMap<_, _> = model
        .views
        .iter()
        .map(|view| (view.id.as_str(), view))
        .collect();
    let equipment: HashSet<_> = model
        .equipment
        .iter()
        .map(|item| item.id.as_str())
        .collect();
    let components: HashSet<_> = model.components.iter().map(|item| item.id).collect();
    let points: HashMap<_, _> = model
        .io_points
        .iter()
        .map(|item| (item.id, item.value_type))
        .collect();
    let mut ids = HashSet::new();
    let mut nodes = 0;
    let mut pipes = 0;
    for view in &model.views {
        let mut reject = |reason| errors.push(invalid(&view.id, reason));
        if !identifier(&view.id) || !ids.insert(&view.id) {
            reject(
                "view identity must be unique and contain 1–96 ASCII letters, digits, or . _ : -"
                    .to_string(),
            );
        }
        if !label(&view.label) {
            reject("view label must contain 1–120 characters".to_string());
        }
        let mut ancestors = HashSet::from([view.id.as_str()]);
        let mut parent = view.parent.as_deref();
        while let Some(id) = parent {
            if !ancestors.insert(id) {
                reject("parent hierarchy contains a cycle".to_string());
                break;
            }
            match views.get(id) {
                Some(view) => parent = view.parent.as_deref(),
                None => {
                    reject(format!("unknown parent {id:?}"));
                    break;
                }
            }
        }
        nodes += view.nodes.len();
        pipes += view.pipes.len();
        if nodes > 512 {
            reject("at most 512 symbols are supported across all views".to_string());
        }
        if pipes > 1024 {
            reject("at most 1024 pipes are supported across all views".to_string());
        }
        let mut node_ids = HashSet::new();
        for node in &view.nodes {
            if !identifier(&node.id) || !node_ids.insert(node.id.as_str()) {
                reject(format!(
                    "symbol identity {:?} is invalid or repeated",
                    node.id
                ));
            }
            if !label(&node.label) {
                reject(format!(
                    "symbol {:?} label must contain 1–120 characters",
                    node.id
                ));
            }
            let (width, height) = node.symbol.dimensions();
            if node.x > 1200 - width || node.y > 760 - height {
                reject(format!(
                    "symbol {:?} extends beyond the 1200 by 760 view",
                    node.id
                ));
            }
            let valid = match (node.symbol, &node.binding) {
                (_, None) => true,
                (PlantViewSymbol::Label, Some(_)) => false,
                (PlantViewSymbol::Measurement, Some(PlantViewBinding::Point(id))) => {
                    points.get(id).is_some_and(|kind| *kind != ValueKind::Bool)
                }
                (PlantViewSymbol::Measurement, Some(_)) => false,
                (_, Some(PlantViewBinding::Equipment(id))) => equipment.contains(id.as_str()),
                (_, Some(PlantViewBinding::Component(id))) => components.contains(id),
                (_, Some(PlantViewBinding::Point(_))) => false,
            };
            if let Some(display) = node.display {
                let numeric = node.symbol == PlantViewSymbol::Measurement
                    && matches!(node.binding, Some(PlantViewBinding::Point(_)));
                let band = display.normal.is_none_or(|[low, high]| {
                    low.is_finite()
                        && high.is_finite()
                        && display.min <= low
                        && low <= high
                        && high <= display.max
                });
                if !numeric
                    || !display.min.is_finite()
                    || !display.max.is_finite()
                    || display.min >= display.max
                    || !band
                {
                    reject(format!(
                        "symbol {:?} has invalid measurement display bounds or binding",
                        node.id
                    ));
                }
            }
            if !valid {
                reject(format!(
                    "symbol {:?} has a missing or incompatible model binding",
                    node.id
                ));
            }
        }
        for pipe in &view.pipes {
            if !node_ids.contains(pipe.from.node.as_str())
                || !node_ids.contains(pipe.to.node.as_str())
                || pipe.from.node == pipe.to.node
            {
                reject("pipe ends must name two different symbols in the same view".to_string());
            }
        }
    }
}

fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 96
        && value
            .bytes()
            .all(|ch| ch.is_ascii_alphanumeric() || b"._:-".contains(&ch))
}

fn label(value: &str) -> bool {
    !value.trim().is_empty() && value.chars().count() <= 120
}
