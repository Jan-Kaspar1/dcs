//! The dynamics document's JSON Schema, emitted as data.
//!
//! [`ProcessElement::json_schema`] returns the draft 2020-12 schema
//! document `dcs-plant-server --dynamics-schema` prints, so non-Rust
//! tooling can check a dynamics document — the JSON list of
//! [`ProcessElement`] declarations `dcs-plant-server
//! --dynamics`/`--check-dynamics` and `dcs-sim-bus-device --dynamics`
//! merge — without linking the Rust merge (decision 40's pattern applied
//! to the decision-24 document). The schema is maintained by hand beside
//! the element types rather than generated from them, and each
//! deliberate divergence from deserialization is recorded here,
//! mirroring `dcs-model`'s schema module.
//!
//! Rules the schema expresses beyond the serde shape:
//!
//! - unknown keys are rejected (`additionalProperties: false`) inside
//!   every element's field object, where serde would silently ignore
//!   them — a misspelled field fails instead of evaporating;
//! - the element wrapper is exactly one externally tagged key
//!   (`minProperties`/`maxProperties` 1 over the declared kind names),
//!   the shape serde's externally tagged enum already requires;
//! - the numeric bounds [`ChannelMap::validate`](crate::ChannelMap) owns,
//!   written down where the vocabulary can express them:
//!   `time_constant`, `damping_ratio`, and `delay` are strictly positive
//!   and `amplitude` is non-negative;
//! - `uniqueItems` on the element list — a sound approximation of the
//!   driven-point conflict rule, since two identical elements
//!   necessarily drive the same output, which the merge rejects as
//!   `ConflictingDriver`;
//! - `seed` is bounded to `u64`, matching the field's type.
//!
//! Rules the schema language cannot express — they stay with
//! deserialization and [`ChannelMap::validate`](crate::ChannelMap) at
//! merge:
//!
//! - point references: every `input`/`inputs`/`output` naming a point
//!   the served channel map binds (`UnknownPoint`) — for the sim-bus
//!   merge, a register address the bank declares — and carrying the
//!   kind the end requires: `float` throughout except a `bool_flow`'s
//!   gate `input` and a `threshold`'s contact `output`, which are
//!   `bool` (`ElementPointKind`, `ElementGateKind`,
//!   `ElementContactKind`);
//! - end direction: the point an element drives must be an `in` point
//!   (`ElementOutputDirection`) — elements model field-side physics
//!   answering commands, so driving an `out` point would rewrite the
//!   command each step; the schema cannot see the served map's
//!   directions;
//! - a `threshold`'s `on`/`off` being distinct (`NonPositiveBand`) —
//!   comparing two fields is beyond the vocabulary;
//! - driven-point uniqueness across differing elements
//!   (`ConflictingDriver`);
//! - finiteness: JSON spells no `NaN`/`inf` literal, but a magnitude
//!   like `1e999` parses as a `number` the merge then rejects as
//!   non-finite;
//! - the lexical integer/float distinction — JSON Schema compares
//!   numbers mathematically, so `1.0` passes an integer field the serde
//!   reader rejects, and an integer past `u64::MAX` passes a point field
//!   the reader rejects as out of range.

use crate::ProcessElement;

/// The schema document's source. The dynamics document deliberately
/// carries no `version` (decision 24), so nothing here is stamped.
const SCHEMA_SOURCE: &str = r##"{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "DCS dynamics document",
  "description": "The plant-side dynamics declaration list dcs-plant-server --dynamics/--check-dynamics and dcs-sim-bus-device --dynamics merge: one externally tagged object per ProcessElement, its single key the snake_case element name. The schema covers element structure, field types, and the numeric bounds it can express; point-binding and merge-time consistency checks remain with ChannelMap::validate (docs/architecture.md).",
  "type": "array",
  "uniqueItems": true,
  "items": { "$ref": "#/$defs/element" },
  "$defs": {
    "id": { "type": "integer", "minimum": 0 },
    "seed": {
      "type": "integer",
      "minimum": 0,
      "maximum": 18446744073709551615
    },
    "positive": { "type": "number", "exclusiveMinimum": 0 },
    "nonnegative": { "type": "number", "minimum": 0 },
    "number": { "type": "number" },
    "element": {
      "type": "object",
      "minProperties": 1,
      "maxProperties": 1,
      "additionalProperties": false,
      "properties": {
        "first_order_lag": { "$ref": "#/$defs/first-order-lag" },
        "second_order_lag": { "$ref": "#/$defs/second-order-lag" },
        "integrator": { "$ref": "#/$defs/integrator" },
        "dead_time": { "$ref": "#/$defs/dead-time" },
        "noise": { "$ref": "#/$defs/noise" },
        "bool_flow": { "$ref": "#/$defs/bool-flow" },
        "flow_sum": { "$ref": "#/$defs/flow-sum" },
        "scaled_flow": { "$ref": "#/$defs/scaled-flow" },
        "threshold": { "$ref": "#/$defs/threshold" }
      }
    },
    "first-order-lag": {
      "type": "object",
      "additionalProperties": false,
      "required": ["input", "output", "time_constant", "initial"],
      "properties": {
        "input": { "$ref": "#/$defs/id" },
        "output": { "$ref": "#/$defs/id" },
        "time_constant": { "$ref": "#/$defs/positive" },
        "initial": { "$ref": "#/$defs/number" }
      }
    },
    "second-order-lag": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "input",
        "output",
        "time_constant",
        "damping_ratio",
        "initial"
      ],
      "properties": {
        "input": { "$ref": "#/$defs/id" },
        "output": { "$ref": "#/$defs/id" },
        "time_constant": { "$ref": "#/$defs/positive" },
        "damping_ratio": { "$ref": "#/$defs/positive" },
        "initial": { "$ref": "#/$defs/number" }
      }
    },
    "integrator": {
      "type": "object",
      "additionalProperties": false,
      "required": ["input", "output", "initial"],
      "properties": {
        "input": { "$ref": "#/$defs/id" },
        "output": { "$ref": "#/$defs/id" },
        "initial": { "$ref": "#/$defs/number" }
      }
    },
    "dead-time": {
      "type": "object",
      "additionalProperties": false,
      "required": ["input", "output", "delay", "initial"],
      "properties": {
        "input": { "$ref": "#/$defs/id" },
        "output": { "$ref": "#/$defs/id" },
        "delay": { "$ref": "#/$defs/positive" },
        "initial": { "$ref": "#/$defs/number" }
      }
    },
    "noise": {
      "type": "object",
      "additionalProperties": false,
      "required": ["input", "output", "amplitude", "seed", "initial"],
      "properties": {
        "input": { "$ref": "#/$defs/id" },
        "output": { "$ref": "#/$defs/id" },
        "amplitude": { "$ref": "#/$defs/nonnegative" },
        "seed": { "$ref": "#/$defs/seed" },
        "initial": { "$ref": "#/$defs/number" }
      }
    },
    "bool-flow": {
      "type": "object",
      "additionalProperties": false,
      "required": ["input", "output", "on_rate", "off_rate", "initial"],
      "properties": {
        "input": { "$ref": "#/$defs/id" },
        "output": { "$ref": "#/$defs/id" },
        "on_rate": { "$ref": "#/$defs/number" },
        "off_rate": { "$ref": "#/$defs/number" },
        "initial": { "$ref": "#/$defs/number" }
      }
    },
    "flow-sum": {
      "type": "object",
      "additionalProperties": false,
      "required": ["inputs", "output", "initial"],
      "properties": {
        "inputs": {
          "type": "array",
          "items": { "$ref": "#/$defs/id" }
        },
        "output": { "$ref": "#/$defs/id" },
        "bias": { "$ref": "#/$defs/number" },
        "initial": { "$ref": "#/$defs/number" }
      }
    },
    "scaled-flow": {
      "type": "object",
      "additionalProperties": false,
      "required": ["input", "output", "gain", "initial"],
      "properties": {
        "input": { "$ref": "#/$defs/id" },
        "output": { "$ref": "#/$defs/id" },
        "gain": { "$ref": "#/$defs/number" },
        "initial": { "$ref": "#/$defs/number" }
      }
    },
    "threshold": {
      "type": "object",
      "additionalProperties": false,
      "required": ["input", "output", "on", "off", "initial"],
      "properties": {
        "input": { "$ref": "#/$defs/id" },
        "output": { "$ref": "#/$defs/id" },
        "on": { "$ref": "#/$defs/number" },
        "off": { "$ref": "#/$defs/number" },
        "initial": { "type": "boolean" }
      }
    }
  }
}"##;

impl ProcessElement {
    /// The JSON Schema (draft 2020-12) for the dynamics document — the
    /// JSON list of `ProcessElement` declarations — as data.
    /// `dcs-plant-server --dynamics-schema` prints its canonical pretty
    /// serialization.
    ///
    /// The schema checks the document's structure, field types, and the
    /// numeric bounds it can express; the point-binding, end-kind, and
    /// driven-point rules the schema language cannot express remain with
    /// [`ChannelMap::validate`](crate::ChannelMap) at merge — see the
    /// module docs for the recorded split.
    pub fn json_schema() -> serde_json::Value {
        serde_json::from_str(SCHEMA_SOURCE)
            .expect("the embedded schema source is fixed JSON and parses")
    }
}
