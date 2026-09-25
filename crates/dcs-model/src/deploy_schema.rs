//! The deployment manifest's JSON Schema, emitted as data.
//!
//! [`deployment_manifest_schema`] returns the draft 2020-12 schema document
//! `dcs-model deploy-schema` prints, so non-Rust tooling can screen a
//! consumer's `deploy/manifest.json` — the declaration binding the model and
//! dynamics artifacts to the pinned release's images and the deployment's
//! controller topology — against the recorded artifact before the
//! rig-definition agreement check runs (docs/release-contract.md's manifest
//! section; decision 96). Unlike the plant-model and dynamics schemas the
//! manifest has no serde type to maintain the schema beside: it is
//! consumer-owned data whose grammar lives in the release contract, so the
//! schema is authored by hand from that documented shape, and the
//! deployment's rig check stays the operative validator.
//!
//! Rules the schema expresses over the documented shape:
//!
//! - unknown keys are rejected (`additionalProperties: false`) at every
//!   level — a misspelled field fails instead of evaporating into the
//!   check-side parser's `get`, which is also what keeps the pair's
//!   `--pair-token` deployment secret out of the document by construction;
//! - every documented required field: `dcs_release`, `images`
//!   (`controller`, `plant_server`), `model` (`path`, `fingerprint`),
//!   `dynamics` (`path` — its `fingerprint` optional), `plant` (`listen`),
//!   and each `controllers` entry's `name` and `listen`;
//! - `dcs_release` carries the release-tag shape `v<X.Y.Z>`;
//! - both fingerprints carry the canonical fingerprint shape — the
//!   sixteen-hex-digit FNV-1a serialization `ModelFingerprint` prints;
//! - `listen` and `standby` carry the `host:port` address shape with a
//!   digit port — the shape the deploy check's port extraction relies on;
//! - `failover_budget` is a positive integer and may appear only on a
//!   tracking standby — `dependentRequired` expresses that the field
//!   qualifies the `standby` declaration, so a duty entry declaring it is
//!   refused;
//! - `controllers` is a nonempty `uniqueItems` list — a sound
//!   approximation of name uniqueness, since identical entries necessarily
//!   share a `name`, so nothing valid is refused;
//! - the optional `topology` section's `pairs` list is nonempty, and each
//!   pair declares a name and exactly two distinct member names —
//!   `minItems`/`maxItems`/`uniqueItems` over the string members is exact
//!   here, not an approximation.
//!
//! Rules the schema language cannot express — the referential half of the
//! contract — stay with the deployment's rig-definition check, which holds
//! the rig definition the rules reference (`rig-mismatch` diagnostics):
//!
//! - a `standby` address naming a declared controller at its declared
//!   listen port;
//! - at most one duty controller — an entry without `standby` — per
//!   deployment: the manifest's one `plant` is one field whose
//!   single-writer claim admits one field-owning run (decision 99),
//!   and a count over an absent key is beyond the vocabulary;
//! - each `members` entry naming a declared `controllers` entry;
//! - memberships disjoint across pairs, and pair names unique among
//!   non-identical pairs;
//! - a pair's standby wiring closing inside it — exactly one member
//!   tracking the other — and a standby edge into a declared pair
//!   belonging to that pair's members;
//! - controller-name uniqueness beyond identical entries;
//! - the lexical integer/float distinction — JSON Schema compares numbers
//!   mathematically, so `3.0` passes `failover_budget` where the rig
//!   check's integer test rejects it;
//! - port ranges and whether a declared address resolves — the schema
//!   pins only the digit-port shape;
//! - every agreement rule between the manifest and its rig definition —
//!   images, mounts, flags, published ports — which is definition-side by
//!   construction.

/// The schema document's source. The manifest carries no version field —
/// the pinned `dcs_release` is its versioning story — so nothing here is
/// stamped.
const SCHEMA_SOURCE: &str = r##"{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "DCS deployment manifest",
  "description": "The consumer-owned deployment declaration: the pinned release, its controller and plant-server images, the model and dynamics artifacts' path/fingerprint pairs (the dynamics fingerprint optional), the plant listen address, the controller entries with their optional standby/failover/persistence fields, and the optional named-pair topology. The schema covers structure, field types, and the membership shape the vocabulary can express; the referential rules — members naming declared controllers, disjoint memberships, wiring closing inside a pair — remain with the deployment's rig check (docs/release-contract.md).",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "dcs_release",
    "images",
    "model",
    "dynamics",
    "plant",
    "controllers"
  ],
  "properties": {
    "dcs_release": {
      "type": "string",
      "pattern": "^v[0-9]+\\.[0-9]+\\.[0-9]+$"
    },
    "images": {
      "type": "object",
      "additionalProperties": false,
      "required": ["controller", "plant_server"],
      "properties": {
        "controller": { "$ref": "#/$defs/image" },
        "plant_server": { "$ref": "#/$defs/image" }
      }
    },
    "model": {
      "type": "object",
      "additionalProperties": false,
      "required": ["path", "fingerprint"],
      "properties": {
        "path": { "$ref": "#/$defs/path" },
        "fingerprint": { "$ref": "#/$defs/fingerprint" }
      }
    },
    "dynamics": {
      "type": "object",
      "additionalProperties": false,
      "required": ["path"],
      "properties": {
        "path": { "$ref": "#/$defs/path" },
        "fingerprint": { "$ref": "#/$defs/fingerprint" }
      }
    },
    "plant": {
      "type": "object",
      "additionalProperties": false,
      "required": ["listen"],
      "properties": {
        "listen": { "$ref": "#/$defs/address" }
      }
    },
    "controllers": {
      "type": "array",
      "minItems": 1,
      "uniqueItems": true,
      "items": { "$ref": "#/$defs/controller" }
    },
    "topology": {
      "type": "object",
      "additionalProperties": false,
      "required": ["pairs"],
      "properties": {
        "pairs": {
          "type": "array",
          "minItems": 1,
          "uniqueItems": true,
          "items": { "$ref": "#/$defs/pair" }
        }
      }
    }
  },
  "$defs": {
    "name": { "type": "string", "minLength": 1 },
    "image": { "type": "string", "minLength": 1 },
    "path": { "type": "string", "minLength": 1 },
    "fingerprint": { "type": "string", "pattern": "^[0-9a-f]{16}$" },
    "address": { "type": "string", "pattern": "^.+:[0-9]{1,5}$" },
    "controller": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "listen"],
      "dependentRequired": { "failover_budget": ["standby"] },
      "properties": {
        "name": { "$ref": "#/$defs/name" },
        "listen": { "$ref": "#/$defs/address" },
        "standby": { "$ref": "#/$defs/address" },
        "failover_budget": { "type": "integer", "minimum": 1 },
        "state_file": { "$ref": "#/$defs/path" },
        "journal_file": { "$ref": "#/$defs/path" }
      }
    },
    "pair": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "members"],
      "properties": {
        "name": { "$ref": "#/$defs/name" },
        "members": {
          "type": "array",
          "minItems": 2,
          "maxItems": 2,
          "uniqueItems": true,
          "items": { "$ref": "#/$defs/name" }
        }
      }
    }
  }
}"##;

/// The deployment manifest's JSON Schema (draft 2020-12) as data.
/// `dcs-model deploy-schema` prints its canonical pretty serialization, so
/// the release record pins its sha256 like the other recorded schemas.
///
/// The schema checks the manifest's structure, field types, and the
/// membership shape it can express; the referential rules the schema
/// language cannot express remain with the deployment's rig-definition
/// check — see the module docs for the recorded split.
pub fn deployment_manifest_schema() -> serde_json::Value {
    serde_json::from_str(SCHEMA_SOURCE)
        .expect("the embedded schema source is fixed JSON and parses")
}
