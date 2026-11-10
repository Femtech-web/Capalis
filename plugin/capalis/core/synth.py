"""Typed placeholder outputs for writes simulated in shadow runs.

Placeholders only let later steps resolve their inputs; they are never presented as observed results.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .schema import Schema, declared_types

SHADOW_PREFIX = "shadow:"


def placeholder(schema: Schema | None, path: str = "output") -> Any:
    if not isinstance(schema, Mapping):
        return None
    if "const" in schema:
        return schema["const"]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    types = declared_types(schema) or frozenset({"object"})
    if "object" in types:
        properties = schema.get("properties") or {}
        return {name: placeholder(sub, f"{path}.{name}") for name, sub in properties.items()}
    if "array" in types:
        return []
    if "string" in types:
        return f"{SHADOW_PREFIX}{path}"
    if "integer" in types or "number" in types:
        # 1 rather than 0: identifiers such as issue numbers start at 1.
        return int(schema.get("minimum", 1))
    if "boolean" in types:
        return False
    return None
