"""A strict, documented subset of JSON Schema.

Capalis-owned schemas (adapter inputs/outputs, trigger payloads) must use only the keywords in
``SUPPORTED_KEYWORDS``; anything else is reported rather than silently ignored, because an ignored
constraint would let invalid runtime data reach a managed effect.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

JSON_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})

SUPPORTED_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum", "const",
    "minLength", "maxLength", "pattern", "minimum", "maximum", "minItems", "maxItems",
    "title", "description", "format", "default",
})

Schema = Mapping[str, Any]


def _shape_problems(schema: Mapping[str, Any], path: str) -> list[str]:
    found: list[str] = []
    raw_type = schema.get("type")
    if raw_type is not None and not (
        (isinstance(raw_type, str) and raw_type in JSON_TYPES)
        or (isinstance(raw_type, list) and raw_type and all(isinstance(t, str) and t in JSON_TYPES for t in raw_type))
    ):
        found.append(f"{path}.type: not a JSON type")
    if "properties" in schema and not (isinstance(schema["properties"], Mapping)
                                       and all(isinstance(k, str) for k in schema["properties"])):
        found.append(f"{path}.properties: must be an object of schemas")
    if "required" in schema and not (isinstance(schema["required"], list)
                                     and all(isinstance(k, str) for k in schema["required"])):
        found.append(f"{path}.required: must be a list of names")
    if "enum" in schema and not isinstance(schema["enum"], list):
        found.append(f"{path}.enum: must be a list")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], (bool, Mapping)):
        found.append(f"{path}.additionalProperties: must be a boolean or schema")
    if "items" in schema and not isinstance(schema["items"], Mapping):
        found.append(f"{path}.items: must be a schema")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (isinstance(schema[key], bool) or not isinstance(schema[key], int) or schema[key] < 0):
            found.append(f"{path}.{key}: must be a non-negative integer")
    for key in ("minimum", "maximum"):
        if key in schema and (isinstance(schema[key], bool) or not isinstance(schema[key], (int, float))):
            found.append(f"{path}.{key}: must be a number")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except (re.error, TypeError):
            found.append(f"{path}.pattern: not a valid regular expression")
    return found


def unsupported_keywords(schema: Any, path: str = "$") -> list[str]:
    """Unsupported keywords and malformed structure; an empty list means the schema is usable."""
    if not isinstance(schema, Mapping):
        return [f"{path}: schema must be an object"]
    found = [f"{path}.{key}" for key in schema if key not in SUPPORTED_KEYWORDS]
    shape = _shape_problems(schema, path)
    found.extend(shape)
    if shape:
        return found
    for name, sub in (schema.get("properties") or {}).items():
        found.extend(unsupported_keywords(sub, f"{path}.properties.{name}"))
    items = schema.get("items")
    if items is not None:
        found.extend(unsupported_keywords(items, f"{path}.items"))
    extra = schema.get("additionalProperties")
    if isinstance(extra, Mapping):
        found.extend(unsupported_keywords(extra, f"{path}.additionalProperties"))
    return found


def declared_types(schema: Any) -> frozenset[str] | None:
    raw = schema.get("type") if isinstance(schema, Mapping) else None
    if raw is None:
        return None
    if isinstance(raw, str):
        return frozenset([raw]) & JSON_TYPES
    if isinstance(raw, list):
        return frozenset(t for t in raw if isinstance(t, str)) & JSON_TYPES
    return frozenset()


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Sequence):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


def _type_matches(value: Any, types: frozenset[str]) -> bool:
    actual = json_type(value)
    return actual in types or (actual == "integer" and "number" in types)


def validate(instance: Any, schema: Schema, path: str = "$") -> list[str]:
    errors: list[str] = []
    types = declared_types(schema)
    if types is not None and not _type_matches(instance, types):
        return [f"{path}: expected {'/'.join(sorted(types))}, got {json_type(instance)}"]
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: must equal the constant value")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: not one of the allowed values")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            errors.append(f"{path}: does not match the required pattern")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: above {schema['maximum']}")
    if isinstance(instance, Mapping):
        properties: Mapping[str, Any] = schema.get("properties") or {}
        for name in schema.get("required") or ():
            if name not in instance:
                errors.append(f"{path}.{name}: required")
        extra = schema.get("additionalProperties", True)
        for name, value in instance.items():
            if name in properties:
                errors.extend(validate(value, properties[name], f"{path}.{name}"))
            elif extra is False:
                errors.append(f"{path}.{name}: not allowed")
            elif isinstance(extra, Mapping):
                errors.extend(validate(value, extra, f"{path}.{name}"))
    if isinstance(instance, Sequence) and not isinstance(instance, str):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, value in enumerate(instance):
                errors.extend(validate(value, items, f"{path}.{index}"))
    return errors


def subschema_at(schema: Schema | None, path: Sequence[str | int]) -> Schema | None:
    """The schema describing the value at ``path``, or None when the path is not declared."""
    current: Any = schema
    for segment in path:
        if not isinstance(current, Mapping):
            return None
        if isinstance(segment, int):
            items = current.get("items")
            current = items if isinstance(items, Mapping) else None
        else:
            properties = current.get("properties")
            current = properties.get(segment) if isinstance(properties, Mapping) else None
    return current if isinstance(current, Mapping) else None


def schema_for_constant(value: Any) -> Schema:
    return {"type": json_type(value)}


def assignable(source: Schema | None, target: Schema | None) -> bool:
    """Whether every value described by ``source`` has a type ``target`` accepts.

    This is a static type check only. Value constraints (enum, length, pattern, bounds) are checked
    against the actual value at runtime before dispatch, which fails closed.
    """
    if target is None or declared_types(target) is None:
        return True
    if source is None:
        return False
    source_types = declared_types(source)
    target_types = declared_types(target)
    if source_types is None or target_types is None:
        return False
    widened = set(target_types) | ({"integer"} if "number" in target_types else set())
    if not source_types <= widened:
        return False
    if "array" in source_types and isinstance(target.get("items"), Mapping):
        return assignable(source.get("items") if isinstance(source.get("items"), Mapping) else None,
                          target["items"])
    if "object" in source_types and target.get("properties"):
        source_props = source.get("properties") or {}
        source_required = set(source.get("required") or ())
        for name in target.get("required") or ():
            if name not in source_required or not assignable(source_props.get(name),
                                                              target["properties"].get(name)):
                return False
    return True
