//! The plant-model document's JSON Schema, emitted as data.
//!
//! [`PlantModel::json_schema`] returns the draft 2020-12 schema document the
//! `dcs-model schema` subcommand prints, so non-Rust tooling can check a
//! model document without linking the Rust validator (decision 3's contract
//! goal). The schema is maintained by hand beside the serde types rather
//! than generated from them: it deliberately diverges from deserialization
//! in both directions where the languages differ in power, and each
//! divergence is recorded here and in `docs/architecture.md`.
//!
//! Rules the schema expresses beyond the serde shape:
//!
//! - `version` is the constant [`MODEL_VERSION`];
//! - unknown keys are rejected (`additionalProperties: false`) where serde
//!   would silently ignore them, so a misspelled field fails instead of
//!   evaporating;
//! - the id-keyed collections (`devices`, `io_points`, `signals`,
//!   `components`) carry `uniqueItems` — an approximation of id uniqueness
//!   that is exact only when duplicated elements are identical;
//! - the internal-point rules: `channel` absent or null requires a non-null
//!   `initial`, a bound point forbids a non-null `initial`, `initial`'s
//!   [`Value`](dcs_core::Value) variant must match `value_type`, and
//!   `writable` may mark `In` points only;
//! - the freshness-budget rule: a non-null `stale_after_ticks` may mark a
//!   field `In` point only — it requires `direction: "in"` and a non-null
//!   `channel`;
//! - the standard registry's known device-kind parameter shapes (decision
//!   29): `sim-tcp` requires `address` and allows `timeout_ms`, `sim-bus`
//!   additionally requires the `registers` map, and `sim-scripted`
//!   requires the `script` map — the parts of each kind's contract JSON
//!   Schema can write down. Other kinds' parameters stay unconstrained
//!   objects: the `sim*` prefix family is open-ended, so the schema does
//!   not pin its parameter rule (the standard `sim` factory rejecting all
//!   parameters is an assembly-side contract, not a document shape).
//!
//! Rules the schema language cannot express — they stay with
//! [`PlantModel::validate`](crate::PlantModel::validate) and the
//! device-kind factories:
//!
//! - cross-references: every channel binding, signal `source`, and
//!   connection endpoint naming a declared device, channel, point,
//!   component, or port;
//! - wiring: a connection `from` end producing and `to` end consuming a
//!   value, and both ends agreeing on the value kind — those depend on the
//!   declared direction and kind of the referenced element;
//! - id uniqueness in general (a duplicated id on otherwise-differing
//!   elements is invisible to `uniqueItems`);
//! - the semantic halves of the device-kind contracts: an `address` that
//!   resolves, a `registers` map covering exactly the declared channels
//!   with no shared register, a `script` keyed to bound `in` channels with
//!   per-entry values matching the channel kind and strictly increasing
//!   ticks;
//! - the lexical integer/float distinction — JSON Schema compares numbers
//!   mathematically, so `1.0` passes an integer field the serde loader
//!   rejects;
//! - the legacy PascalCase spellings of [`Value`](dcs_core::Value) and
//!   [`ValueKind`](dcs_core::ValueKind) variants — the serde loader still
//!   accepts `"Bool"`/`"Int"`/`"Float"` through its read-compat aliases,
//!   while the schema pins the canonical `snake_case` vocabulary the
//!   contract emits.

use crate::PlantModel;
use crate::model::MODEL_VERSION;

/// The schema document's source. `version`'s `const` is stamped at parse
/// time from [`MODEL_VERSION`], so the two cannot drift.
const SCHEMA_SOURCE: &str = r##"{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "DCS plant model",
  "description": "The versioned plant-model document: devices, logical io_points, signals, components, and connections. The schema covers structure, field types, and intra-element rules; cross-reference and wiring checks remain with the Rust validator (docs/architecture.md).",
  "type": "object",
  "additionalProperties": false,
  "required": ["version", "devices", "io_points", "signals", "components", "connections"],
  "properties": {
    "version": { "const": 0 },
    "devices": {
      "type": "array",
      "uniqueItems": true,
      "items": { "$ref": "#/$defs/device" }
    },
    "io_points": {
      "type": "array",
      "uniqueItems": true,
      "items": { "$ref": "#/$defs/io-point" }
    },
    "signals": {
      "type": "array",
      "uniqueItems": true,
      "items": { "$ref": "#/$defs/signal" }
    },
    "components": {
      "type": "array",
      "uniqueItems": true,
      "items": { "$ref": "#/$defs/component" }
    },
    "connections": {
      "type": "array",
      "items": { "$ref": "#/$defs/connection" }
    }
  },
  "$defs": {
    "id": { "type": "integer", "minimum": 0 },
    "direction": { "enum": ["in", "out"] },
    "value-kind": { "enum": ["bool", "int", "float"] },
    "nonneg-int": { "type": "integer", "minimum": 0 },
    "register-index": { "type": "integer", "minimum": 0, "maximum": 65535 },
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
    "endpoint-shape": {
      "type": "object",
      "additionalProperties": false,
      "required": ["direction", "value_type"],
      "properties": {
        "direction": { "$ref": "#/$defs/direction" },
        "value_type": { "$ref": "#/$defs/value-kind" }
      }
    },
    "channel-ref": {
      "type": "object",
      "additionalProperties": false,
      "required": ["device", "name"],
      "properties": {
        "device": { "$ref": "#/$defs/id" },
        "name": { "type": "string" }
      }
    },
    "port-ref": {
      "type": "object",
      "additionalProperties": false,
      "required": ["component", "name"],
      "properties": {
        "component": { "$ref": "#/$defs/id" },
        "name": { "type": "string" }
      }
    },
    "endpoint": {
      "oneOf": [
        {
          "type": "object",
          "additionalProperties": false,
          "required": ["point"],
          "properties": { "point": { "$ref": "#/$defs/id" } }
        },
        {
          "type": "object",
          "additionalProperties": false,
          "required": ["port"],
          "properties": { "port": { "$ref": "#/$defs/port-ref" } }
        }
      ]
    },
    "device": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "kind", "channels"],
      "properties": {
        "id": { "$ref": "#/$defs/id" },
        "kind": { "type": "string" },
        "channels": {
          "type": "object",
          "additionalProperties": { "$ref": "#/$defs/endpoint-shape" }
        },
        "parameters": { "type": "object" }
      },
      "allOf": [
        {
          "if": {
            "required": ["kind"],
            "properties": { "kind": { "const": "sim-tcp" } }
          },
          "then": {
            "required": ["parameters"],
            "properties": {
              "parameters": {
                "type": "object",
                "additionalProperties": false,
                "required": ["address"],
                "properties": {
                  "address": { "type": "string" },
                  "timeout_ms": { "$ref": "#/$defs/nonneg-int" }
                }
              }
            }
          }
        },
        {
          "if": {
            "required": ["kind"],
            "properties": { "kind": { "const": "sim-bus" } }
          },
          "then": {
            "required": ["parameters"],
            "properties": {
              "parameters": {
                "type": "object",
                "additionalProperties": false,
                "required": ["address", "registers"],
                "properties": {
                  "address": { "type": "string" },
                  "timeout_ms": { "$ref": "#/$defs/nonneg-int" },
                  "registers": {
                    "type": "object",
                    "additionalProperties": {
                      "anyOf": [
                        { "$ref": "#/$defs/register-index" },
                        {
                          "type": "object",
                          "additionalProperties": false,
                          "required": ["register"],
                          "properties": {
                            "register": { "$ref": "#/$defs/register-index" },
                            "initial": { "$ref": "#/$defs/value" }
                          }
                        }
                      ]
                    }
                  }
                }
              }
            }
          }
        },
        {
          "if": {
            "required": ["kind"],
            "properties": { "kind": { "const": "sim-scripted" } }
          },
          "then": {
            "required": ["parameters"],
            "properties": {
              "parameters": {
                "type": "object",
                "additionalProperties": false,
                "required": ["script"],
                "properties": {
                  "script": {
                    "type": "object",
                    "additionalProperties": {
                      "type": "array",
                      "items": { "$ref": "#/$defs/script-entry" }
                    }
                  }
                }
              }
            }
          }
        }
      ]
    },
    "script-entry": {
      "type": "object",
      "additionalProperties": false,
      "required": ["tick", "value"],
      "properties": {
        "tick": { "$ref": "#/$defs/nonneg-int" },
        "value": true,
        "quality": { "enum": ["good", "uncertain", "bad"] },
        "reason": {
          "enum": [
            "unspecified",
            "substituted",
            "stale",
            "out_of_range",
            "communication_fault",
            "device_fault",
            "configuration_fault"
          ]
        }
      },
      "allOf": [
        {
          "if": { "required": ["reason"] },
          "then": {
            "required": ["quality"],
            "properties": { "quality": { "enum": ["uncertain", "bad"] } }
          }
        }
      ]
    },
    "io-point": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "direction", "value_type"],
      "properties": {
        "id": { "$ref": "#/$defs/id" },
        "direction": { "$ref": "#/$defs/direction" },
        "value_type": { "$ref": "#/$defs/value-kind" },
        "channel": {
          "anyOf": [{ "$ref": "#/$defs/channel-ref" }, { "type": "null" }]
        },
        "initial": {
          "anyOf": [{ "$ref": "#/$defs/value" }, { "type": "null" }]
        },
        "writable": { "type": "boolean" },
        "stale_after_ticks": {
          "anyOf": [{ "$ref": "#/$defs/nonneg-int" }, { "type": "null" }]
        }
      },
      "allOf": [
        {
          "if": {
            "anyOf": [
              { "not": { "required": ["channel"] } },
              {
                "required": ["channel"],
                "properties": { "channel": { "type": "null" } }
              }
            ]
          },
          "then": {
            "required": ["initial"],
            "properties": { "initial": { "$ref": "#/$defs/value" } }
          }
        },
        {
          "if": {
            "required": ["channel"],
            "properties": { "channel": { "not": { "type": "null" } } }
          },
          "then": {
            "properties": { "initial": { "type": "null" } }
          }
        },
        {
          "if": {
            "required": ["writable"],
            "properties": { "writable": { "const": true } }
          },
          "then": {
            "properties": { "direction": { "const": "in" } }
          }
        },
        {
          "if": {
            "required": ["stale_after_ticks"],
            "properties": { "stale_after_ticks": { "type": "integer" } }
          },
          "then": {
            "required": ["channel"],
            "properties": {
              "direction": { "const": "in" },
              "channel": { "not": { "type": "null" } }
            }
          }
        },
        {
          "if": {
            "required": ["initial", "value_type"],
            "properties": {
              "value_type": { "const": "bool" },
              "initial": { "type": "object" }
            }
          },
          "then": {
            "properties": { "initial": { "$ref": "#/$defs/bool-value" } }
          }
        },
        {
          "if": {
            "required": ["initial", "value_type"],
            "properties": {
              "value_type": { "const": "int" },
              "initial": { "type": "object" }
            }
          },
          "then": {
            "properties": { "initial": { "$ref": "#/$defs/int-value" } }
          }
        },
        {
          "if": {
            "required": ["initial", "value_type"],
            "properties": {
              "value_type": { "const": "float" },
              "initial": { "type": "object" }
            }
          },
          "then": {
            "properties": { "initial": { "$ref": "#/$defs/float-value" } }
          }
        }
      ]
    },
    "signal": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "name", "source"],
      "properties": {
        "id": { "$ref": "#/$defs/id" },
        "name": { "type": "string" },
        "source": { "$ref": "#/$defs/id" },
        "unit": { "type": ["string", "null"] },
        "description": { "type": ["string", "null"] },
        "group": { "type": ["string", "null"] }
      }
    },
    "component": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "kind", "parameters", "ports"],
      "properties": {
        "id": { "$ref": "#/$defs/id" },
        "kind": { "type": "string" },
        "parameters": {
          "type": "object",
          "additionalProperties": { "$ref": "#/$defs/value" }
        },
        "ports": {
          "type": "object",
          "additionalProperties": { "$ref": "#/$defs/endpoint-shape" }
        }
      }
    },
    "connection": {
      "type": "object",
      "additionalProperties": false,
      "required": ["from", "to"],
      "properties": {
        "from": { "$ref": "#/$defs/endpoint" },
        "to": { "$ref": "#/$defs/endpoint" }
      }
    }
  }
}"##;

impl PlantModel {
    /// The JSON Schema (draft 2020-12) for the plant-model document, as
    /// data. `dcs-model schema` prints its canonical pretty serialization.
    ///
    /// The schema checks document structure, field types, and the
    /// intra-element and known device-kind rules the schema language can
    /// express; cross-reference and wiring rules remain with
    /// [`PlantModel::load`](crate::PlantModel::load) — see the module docs
    /// for the recorded split.
    pub fn json_schema() -> serde_json::Value {
        let mut schema: serde_json::Value = serde_json::from_str(SCHEMA_SOURCE)
            .expect("the embedded schema source is fixed JSON and parses");
        schema["properties"]["version"]["const"] = MODEL_VERSION.into();
        schema
    }
}
