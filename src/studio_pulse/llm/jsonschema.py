"""A small JSON Schema validator (the subset structured LLM output needs) and a tolerant JSON reader.

Supported keywords: type (or a list of types), enum, const, properties, required,
additionalProperties (bool or schema), items, minItems, maxItems, minLength, maxLength,
minimum, maximum, pattern, anyOf. No external dependency, so the same validation runs on
Windows, macOS and Linux without a compiled package.
"""

from __future__ import annotations

import json
import re
from typing import Any

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return every problem found (an empty list means valid)."""
    errs: list[str] = []
    if not schema:
        return errs
    if "anyOf" in schema:
        options = [validate(value, s, path) for s in schema["anyOf"]]
        if all(options):
            errs.append(f"{path}: matches none of anyOf ({options[0][0]})")
        return errs
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_TYPES.get(x, lambda v: False)(value) for x in types):
            return [f"{path}: expected {'/'.join(types)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        errs.append(f"{path}: must be {schema['const']!r}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errs.append(f"{path}: does not match {schema['pattern']}")
    if _TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: above {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{path}: more than {schema['maxItems']} items")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                errs += validate(item, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for k in schema.get("required") or []:
            if k not in value:
                errs.append(f"{path}: missing {k!r}")
        extra = schema.get("additionalProperties", True)
        for k, v in value.items():
            if k in props:
                errs += validate(v, props[k], f"{path}.{k}")
            elif extra is False:
                errs.append(f"{path}: unexpected key {k!r}")
            elif isinstance(extra, dict):
                errs += validate(v, extra, f"{path}.{k}")
    return errs


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def parse_json_text(text: str) -> Any:
    """Parse a model's JSON answer; tolerates a Markdown fence and text around one object."""
    s = text.strip()
    m = _FENCE.match(s)
    if m:
        s = m.group(1)
    try:
        return json.loads(s)
    except ValueError:
        start, end = s.find("{"), s.rfind("}")
        if start >= 0 and end > start:
            return json.loads(s[start : end + 1])
        raise


def minimal_instance(schema: dict[str, Any]) -> Any:
    """The smallest value that satisfies `schema` (the fake provider's default answer)."""
    if "anyOf" in schema:
        return minimal_instance(schema["anyOf"][0])
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type")
    t = t[0] if isinstance(t, list) else t
    if t == "object":
        props = schema.get("properties") or {}
        return {k: minimal_instance(props.get(k, {})) for k in schema.get("required") or []}
    if t == "array":
        n = int(schema.get("minItems", 0))
        return [minimal_instance(schema.get("items") or {}) for _ in range(n)]
    if t == "string":
        return "x" * int(schema.get("minLength", 0))
    if t in ("integer", "number"):
        return schema.get("minimum", 0)
    if t == "boolean":
        return False
    return None
