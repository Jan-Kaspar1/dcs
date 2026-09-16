#!/usr/bin/env python3
"""Structural conformance of a served registry document against the
release record's served-registry JSON Schema.

`ci/check.sh`'s surface stage drives the deterministic run whose
`GET /schema` answer this script checks against the release record's
`block-interfaces.schema.json` — the draft 2020-12 artifact `dcs-model
interface-schema` emits, fetched through the pinned revision's git
remote and pinned byte-identical to the pinned tooling's emission by
the tooling stage's `schema-drift` leg.

The boundary this check documents: it is a required-keys/field-shape
conformance check in stdlib-only python. Required keys must be
present, declared properties and items recurse, fields carry their
declared JSON `type`s, `enum`/`const` vocabularies hold,
`additionalProperties: false` is enforced, `minimum`/`maximum` bounds
apply, and local `$ref`/`anyOf`/`oneOf` combinators resolve through
the artifact's own `$defs`. Full draft 2020-12 validation — every
keyword the draft defines, including `uniqueItems`, `pattern`, and
format/annotation semantics — stays with the platform's own checks,
where the `jsonschema` dependency exists: a consumer needs no
third-party dependency to catch a served surface that dropped or
mistyped a required field, which is the failure this check reports as
`schema-mismatch`.

Usage:

    schema_conformance.py --schema block-interfaces.schema.json \
        --document served-schema.json

On success one summary line prints and the exit status is 0. Each
structural failure is a `schema-mismatch: <path>: <detail>` line on
stderr and the exit status is 1.
"""

import argparse
import json
import sys


def eprint(*args):
    print(*args, file=sys.stderr)


def die(diagnostic, detail):
    print(f"{diagnostic}: {detail}", file=sys.stderr)
    sys.exit(1)


def load(path, what):
    try:
        with open(path) as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        die("schema-mismatch", f"the {what} at {path} does not parse: {error}")
    if not isinstance(document, dict):
        die("schema-mismatch", f"the {what} at {path} is not an object")
    return document


def json_equal(left, right):
    """Schema-level equality: a boolean is not the integer 1, while
    `1` and `1.0` are the same number."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


def type_holds(declared, value):
    """Whether `value` carries the declared JSON Schema `type`."""
    if declared == "object":
        return isinstance(value, dict)
    if declared == "array":
        return isinstance(value, list)
    if declared == "string":
        return isinstance(value, str)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "null":
        return value is None
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True  # an unrecognized type name is outside this check's vocabulary


def resolve(node, root):
    """Resolves a local `$ref` (`#/…` JSON Pointer) against the
    artifact's own document — the `#/$defs/<name>` form the emitted
    schema uses."""
    seen = set()
    while isinstance(node, dict) and "$ref" in node:
        if id(node) in seen:
            return None
        seen.add(id(node))
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return None
        target = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                return None
            target = target[part]
        node = target
    return node


def matches(node, value, root):
    """Whether `value` conforms to `node` — the sub-verdict `anyOf` and
    `oneOf` alternatives need."""
    inner = []
    check(node, value, "$", root, inner)
    return not inner


def check(node, value, path, root, out):
    """Appends one `<path>: <detail>` mismatch per structural rule the
    document violates. `path` is the document position (`$.a.b[2]`) the
    diagnostic names."""
    node = resolve(node, root)
    if not isinstance(node, dict):
        out.append(f"{path}: the artifact's reference does not resolve")
        return
    if "const" in node and not json_equal(node["const"], value):
        out.append(f"{path}: is {value!r}, the schema requires {node['const']!r}")
    if "enum" in node and not any(
        json_equal(member, value) for member in node["enum"]
    ):
        out.append(f"{path}: {value!r} is not one of {node['enum']!r}")
    declared = node.get("type")
    if declared is not None:
        types = declared if isinstance(declared, list) else [declared]
        if not any(type_holds(kind, value) for kind in types):
            out.append(f"{path}: {value!r} is not of type {declared!r}")
            return
    if "anyOf" in node and not any(
        matches(sub, value, root) for sub in node["anyOf"]
    ):
        out.append(f"{path}: {value!r} matches no anyOf alternative")
    if "oneOf" in node:
        hits = sum(matches(sub, value, root) for sub in node["oneOf"])
        if hits != 1:
            out.append(
                f"{path}: {value!r} matches {hits} oneOf alternatives, "
                "the schema requires exactly one"
            )
    if isinstance(value, dict):
        for key in node.get("required", []):
            if key not in value:
                out.append(f"{path}: required field {key!r} is absent")
        properties = node.get("properties", {})
        if node.get("additionalProperties") is False:
            for key in sorted(set(value) - set(properties)):
                out.append(f"{path}: field {key!r} is not in the schema")
        for key, sub in properties.items():
            if key in value:
                check(sub, value[key], f"{path}.{key}", root, out)
    if isinstance(value, list):
        items = node.get("items")
        if items is not None:
            for index, item in enumerate(value):
                check(items, item, f"{path}[{index}]", root, out)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in node and value < node["minimum"]:
            out.append(f"{path}: {value!r} is below minimum {node['minimum']!r}")
        if "maximum" in node and value > node["maximum"]:
            out.append(f"{path}: {value!r} is above maximum {node['maximum']!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--schema",
        required=True,
        help="the release record's block-interfaces.schema.json",
    )
    parser.add_argument(
        "--document",
        required=True,
        help="the served GET /schema document to check",
    )
    args = parser.parse_args()
    artifact = load(args.schema, "recorded schema artifact")
    document = load(args.document, "served document")
    mismatches = []
    check(artifact, document, "$", artifact, mismatches)
    if mismatches:
        for mismatch in mismatches:
            eprint(f"schema-mismatch: {mismatch}")
        sys.exit(1)
    interfaces = document.get("interfaces")
    count = len(interfaces) if isinstance(interfaces, list) else 0
    print(
        f"the served document conforms to the recorded schema artifact — "
        f"{count} interfaces checked"
    )


if __name__ == "__main__":
    main()
