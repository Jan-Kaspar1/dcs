//! The served block-interface registry's JSON Schema, emitted as data.
//!
//! [`SchemaView::json_schema`] returns the draft 2020-12 schema document the
//! `dcs-model interface-schema` subcommand prints, so non-Rust tooling can
//! check a served [`SchemaView`] — the document `GET /schema` answers — or
//! an offline capture of one without linking the Rust contract (decision
//! 40's pattern applied to the decision-82 registry). The schema is
//! maintained by hand beside the serde types rather than generated from
//! them, and each deliberate divergence from deserialization is recorded
//! here, mirroring `dcs-model`'s schema module.
//!
//! Rules the schema expresses beyond the serde shape:
//!
//! - each interface's `version` is the constant
//!   [`INTERFACE_VERSION`](crate::INTERFACE_VERSION), stamped at parse
//!   time so the two cannot drift — the version field's compatibility
//!   rule mirrors `MODEL_VERSION`'s: a document carrying any other
//!   version is outside the contract this schema describes;
//! - unknown keys are rejected (`additionalProperties: false`) where
//!   serde's additive convention would silently ignore them, so a
//!   misspelled field fails instead of evaporating — a document written
//!   by a newer minor version carrying fields this version does not know
//!   is the newer release's schema to check;
//! - `uniqueItems` on `interfaces` and on each of the five collections —
//!   an approximation of name uniqueness that is exact only when
//!   duplicated elements are identical;
//! - the declared vocabularies: [`Direction`](crate::Direction),
//!   [`ValueKind`](crate::ValueKind), [`PortRole`](crate::PortRole),
//!   [`StatePersistence`](crate::StatePersistence),
//!   [`ConfigCapability`](crate::ConfigCapability),
//!   [`CommandAvailability`](crate::CommandAvailability),
//!   [`AdaptedCommand`](crate::AdaptedCommand),
//!   [`EventRetention`](crate::EventRetention),
//!   [`EventEmission`](crate::EventEmission),
//!   [`AdaptedEvent`](crate::AdaptedEvent), and
//!   [`EventFieldKind`](crate::EventFieldKind)'s `{"value": kind}` /
//!   `"quality"` / `"receipt"` / `"text"` payload-type shape;
//! - `Option` fields (`role`, `point`, `unit`, `range`) admit `null`,
//!   matching serde, though the emitted documents carry them only when
//!   populated.
//!
//! Rules the schema language cannot express — they stay with the
//! descriptor/derivation contract and the serving layer:
//!
//! - cross-collection consistency: a `set_parameter:<name>` command
//!   naming a `configuration` entry, a `bound_point_writable` command
//!   carrying a bound `point` in the served instance-level form, a
//!   `when_journaled` event adapting a `Bool`/`Int` port, a request or
//!   payload argument's kind matching its target's — the
//!   [`BlockInterface::from_descriptor`](crate::BlockInterface::from_descriptor)
//!   adaptation produces these pairs, so a served document always
//!   satisfies them, but a hand-written one can violate them;
//! - name uniqueness in general (a duplicated resource name on
//!   otherwise-differing elements is invisible to `uniqueItems`);
//! - the lexical integer/float distinction — JSON Schema compares
//!   numbers mathematically, so `1.0` passes an `int` field the serde
//!   reader rejects;
//! - the legacy PascalCase spellings of [`Value`](crate::Value) and
//!   [`ValueKind`](crate::ValueKind) variants — the serde reader still
//!   accepts `"Bool"`/`"Int"`/`"Float"` through its read-compat aliases,
//!   while the schema pins the canonical `snake_case` vocabulary the
//!   contract emits;
//! - that the registry corresponds to a live run at all — `publication`
//!   and `tick` identify the read model the view was derived from, and
//!   joining them to a concurrently fetched `/snapshot` is a runtime
//!   check, not a document shape.

use crate::interface::INTERFACE_VERSION;
use crate::resources::SchemaView;

/// The schema document's source. `version`'s `const` is stamped at parse
/// time from [`INTERFACE_VERSION`], so the two cannot drift.
const SCHEMA_SOURCE: &str = r##"{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "DCS served block-interface registry",
  "description": "The document GET /schema serves: one instance-level BlockInterface per served component instance — decision 82's five resource collections in served form. The schema covers structure, field types, and the declared vocabularies; that the registry corresponds to a live run's instances stays with the serving layer (docs/architecture.md).",
  "type": "object",
  "additionalProperties": false,
  "required": ["publication", "tick", "interfaces"],
  "properties": {
    "publication": { "type": "integer", "minimum": 0 },
    "tick": { "type": "integer", "minimum": 0 },
    "interfaces": {
      "type": "array",
      "uniqueItems": true,
      "items": { "$ref": "#/$defs/component-interface" }
    }
  },
  "$defs": {
    "id": { "type": "integer", "minimum": 0 },
    "direction": { "enum": ["in", "out"] },
    "value-kind": { "enum": ["bool", "int", "float"] },
    "port-role": {
      "enum": ["process_value", "setpoint", "output", "status"]
    },
    "state-persistence": { "enum": ["bound_point"] },
    "config-capability": { "enum": ["tunable", "engineering_fixed"] },
    "command-availability": {
      "enum": ["always", "bound_point_writable", "kind_declared"]
    },
    "adapted-command": {
      "enum": [
        "write_value",
        "force_point",
        "unforce_point",
        "set_parameter",
        "declared"
      ]
    },
    "event-retention": { "enum": ["journal", "history", "latest"] },
    "event-emission": {
      "enum": [
        "on_observed_change",
        "when_journaled",
        "on_command_settled",
        "on_step_failure",
        "kind_emitted"
      ]
    },
    "adapted-event": {
      "enum": [
        "point_changed",
        "quality_changed",
        "command_settled",
        "step_failed",
        "declared"
      ]
    },
    "bool-value": {
      "type": "object",
      "additionalProperties": false,
      "required": ["bool"],
      "properties": { "bool": { "type": "boolean" } }
    },
    "int-value": {
      "type": "object",
      "additionalProperties": false,
      "required": ["int"],
      "properties": { "int": { "type": "integer" } }
    },
    "float-value": {
      "type": "object",
      "additionalProperties": false,
      "required": ["float"],
      "properties": { "float": { "type": "number" } }
    },
    "value": {
      "oneOf": [
        { "$ref": "#/$defs/bool-value" },
        { "$ref": "#/$defs/int-value" },
        { "$ref": "#/$defs/float-value" }
      ]
    },
    "parameter-range": {
      "type": "object",
      "additionalProperties": false,
      "required": ["min", "max"],
      "properties": {
        "min": { "$ref": "#/$defs/value" },
        "max": { "$ref": "#/$defs/value" }
      }
    },
    "event-field-kind": {
      "oneOf": [
        {
          "type": "object",
          "additionalProperties": false,
          "required": ["value"],
          "properties": { "value": { "$ref": "#/$defs/value-kind" } }
        },
        { "enum": ["quality", "receipt", "text"] }
      ]
    },
    "point-or-null": {
      "anyOf": [{ "$ref": "#/$defs/id" }, { "type": "null" }]
    },
    "role-or-null": {
      "anyOf": [{ "$ref": "#/$defs/port-role" }, { "type": "null" }]
    },
    "command-argument": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "kind"],
      "properties": {
        "name": { "type": "string" },
        "kind": { "$ref": "#/$defs/value-kind" }
      }
    },
    "event-field": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "kind"],
      "properties": {
        "name": { "type": "string" },
        "kind": { "$ref": "#/$defs/event-field-kind" },
        "optional": { "type": "boolean" }
      }
    },
    "measurement": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "direction", "kind"],
      "properties": {
        "name": { "type": "string" },
        "direction": { "$ref": "#/$defs/direction" },
        "kind": { "$ref": "#/$defs/value-kind" },
        "role": { "$ref": "#/$defs/role-or-null" },
        "point": { "$ref": "#/$defs/point-or-null" },
        "unit": { "type": ["string", "null"] }
      }
    },
    "state-property": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "direction", "kind", "persistence"],
      "properties": {
        "name": { "type": "string" },
        "direction": { "$ref": "#/$defs/direction" },
        "kind": { "$ref": "#/$defs/value-kind" },
        "role": { "$ref": "#/$defs/role-or-null" },
        "point": { "$ref": "#/$defs/point-or-null" },
        "persistence": { "$ref": "#/$defs/state-persistence" }
      }
    },
    "config-property": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "kind", "capability"],
      "properties": {
        "name": { "type": "string" },
        "kind": { "$ref": "#/$defs/value-kind" },
        "range": {
          "anyOf": [{ "$ref": "#/$defs/parameter-range" }, { "type": "null" }]
        },
        "capability": { "$ref": "#/$defs/config-capability" }
      }
    },
    "command-spec": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "request", "availability", "adapted"],
      "properties": {
        "name": { "type": "string" },
        "request": {
          "type": "array",
          "items": { "$ref": "#/$defs/command-argument" }
        },
        "availability": { "$ref": "#/$defs/command-availability" },
        "adapted": { "$ref": "#/$defs/adapted-command" },
        "point": { "$ref": "#/$defs/point-or-null" }
      }
    },
    "event-spec": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "payload", "retention", "emission", "adapted"],
      "properties": {
        "name": { "type": "string" },
        "payload": {
          "type": "array",
          "items": { "$ref": "#/$defs/event-field" }
        },
        "retention": { "$ref": "#/$defs/event-retention" },
        "emission": { "$ref": "#/$defs/event-emission" },
        "adapted": { "$ref": "#/$defs/adapted-event" },
        "point": { "$ref": "#/$defs/point-or-null" }
      }
    },
    "block-interface": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "version",
        "kind",
        "measurements",
        "configuration",
        "state",
        "commands",
        "events"
      ],
      "properties": {
        "version": { "const": 0 },
        "kind": { "type": "string" },
        "measurements": {
          "type": "array",
          "uniqueItems": true,
          "items": { "$ref": "#/$defs/measurement" }
        },
        "configuration": {
          "type": "array",
          "uniqueItems": true,
          "items": { "$ref": "#/$defs/config-property" }
        },
        "state": {
          "type": "array",
          "uniqueItems": true,
          "items": { "$ref": "#/$defs/state-property" }
        },
        "commands": {
          "type": "array",
          "uniqueItems": true,
          "items": { "$ref": "#/$defs/command-spec" }
        },
        "events": {
          "type": "array",
          "uniqueItems": true,
          "items": { "$ref": "#/$defs/event-spec" }
        }
      }
    },
    "component-interface": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "interface"],
      "properties": {
        "name": { "type": "string" },
        "interface": { "$ref": "#/$defs/block-interface" }
      }
    }
  }
}"##;

impl SchemaView {
    /// The JSON Schema (draft 2020-12) for the served block-interface
    /// registry document, as data. `dcs-model interface-schema` prints
    /// its canonical pretty serialization.
    ///
    /// The schema checks the registry document's structure, field types,
    /// and declared vocabularies; the cross-collection and derivation
    /// rules the schema language cannot express remain with
    /// [`BlockInterface::from_descriptor`](crate::BlockInterface::from_descriptor)
    /// and the serving layer — see the module docs for the recorded
    /// split.
    pub fn json_schema() -> serde_json::Value {
        let mut schema: serde_json::Value = serde_json::from_str(SCHEMA_SOURCE)
            .expect("the embedded schema source is fixed JSON and parses");
        schema["$defs"]["block-interface"]["properties"]["version"]["const"] =
            INTERFACE_VERSION.into();
        schema
    }
}
