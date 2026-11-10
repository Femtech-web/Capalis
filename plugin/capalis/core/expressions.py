"""Value references, templates and bounded conditions.

This is the only place runtime data (trigger payloads, step outputs) meets a compiled Recipe. Every
construct here *reads* data; none can name a capability, add a step or change a target, which is how
I-01 holds even for hostile payloads.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

Path: TypeAlias = tuple[str | int, ...]

MAX_PATH_DEPTH = 8
MAX_CONDITION_DEPTH = 4
MAX_CONDITION_NODES = 32
MAX_TEMPLATE_LENGTH = 4000

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INDEX = re.compile(r"0|[1-9][0-9]{0,5}")


class ExpressionError(ValueError):
    pass


class _Missing:
    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Any = _Missing()


@dataclass(frozen=True, slots=True)
class Ref:
    root: Literal["trigger", "steps"]
    step_id: str | None
    path: Path

    def text(self) -> str:
        parts: list[str] = [self.root] + ([self.step_id] if self.step_id else [])
        parts.extend(str(p) for p in self.path)
        return ".".join(parts)


@dataclass(frozen=True, slots=True)
class Const:
    value: Any


@dataclass(frozen=True, slots=True)
class Template:
    parts: tuple[str | Ref, ...]


@dataclass(frozen=True, slots=True)
class FirstOf:
    options: tuple[Ref, ...]


ValueRef: TypeAlias = Const | Ref | Template | FirstOf


@dataclass(frozen=True, slots=True)
class Compare:
    op: Literal["exists", "missing", "truthy", "equals", "not_equals", "in"]
    ref: Ref
    value: Any = None


@dataclass(frozen=True, slots=True)
class AllOf:
    items: tuple[Condition, ...]


@dataclass(frozen=True, slots=True)
class AnyOf:
    items: tuple[Condition, ...]


@dataclass(frozen=True, slots=True)
class Not:
    item: Condition


Condition: TypeAlias = Compare | AllOf | AnyOf | Not


def parse_ref(text: Any) -> Ref:
    if not isinstance(text, str) or not text:
        raise ExpressionError("reference must be a non-empty string")
    segments = text.split(".")
    root = segments[0]
    if root == "trigger":
        step_id, rest = None, segments[1:]
    elif root == "steps":
        if len(segments) < 2 or not _IDENT.fullmatch(segments[1]):
            raise ExpressionError(f"{text!r}: steps references need a step id")
        step_id, rest = segments[1], segments[2:]
    else:
        raise ExpressionError(f"{text!r}: references start with 'trigger.' or 'steps.<id>.'")
    if len(rest) > MAX_PATH_DEPTH:
        raise ExpressionError(f"{text!r}: path deeper than {MAX_PATH_DEPTH}")
    path: list[str | int] = []
    for segment in rest:
        if _INDEX.fullmatch(segment):
            path.append(int(segment))
        elif _IDENT.fullmatch(segment):
            path.append(segment)
        else:
            raise ExpressionError(f"{text!r}: invalid path segment {segment!r}")
    if root == "trigger" and not path:
        raise ExpressionError(f"{text!r}: reference a field of the trigger payload")
    return Ref(root="trigger" if root == "trigger" else "steps", step_id=step_id, path=tuple(path))


def parse_template(text: Any) -> Template:
    if not isinstance(text, str):
        raise ExpressionError("template must be a string")
    if len(text) > MAX_TEMPLATE_LENGTH:
        raise ExpressionError(f"template longer than {MAX_TEMPLATE_LENGTH} characters")
    parts: list[str | Ref] = []
    buffer: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "{" and text.startswith("{{", i):
            buffer.append("{")
            i += 2
        elif char == "}" and text.startswith("}}", i):
            buffer.append("}")
            i += 2
        elif char == "{":
            end = text.find("}", i)
            if end < 0:
                raise ExpressionError("unclosed '{' in template")
            if buffer:
                parts.append("".join(buffer))
                buffer = []
            parts.append(parse_ref(text[i + 1:end].strip()))
            i = end + 1
        elif char == "}":
            raise ExpressionError("unmatched '}' in template; write '}}' for a literal brace")
        else:
            buffer.append(char)
            i += 1
    if buffer:
        parts.append("".join(buffer))
    return Template(parts=tuple(parts))


def parse_value(raw: Any) -> ValueRef:
    if not isinstance(raw, Mapping) or len(raw) != 1:
        raise ExpressionError("a value is one of {'const': ...}, {'from': ...}, {'template': ...}, "
                              "{'first_of': [...]}")
    (kind, payload), = raw.items()
    if kind == "const":
        return Const(payload)
    if kind == "from":
        return parse_ref(payload)
    if kind == "template":
        return parse_template(payload)
    if kind == "first_of":
        if not isinstance(payload, list) or not 2 <= len(payload) <= 4:
            raise ExpressionError("first_of takes 2 to 4 references")
        return FirstOf(tuple(parse_ref(item) for item in payload))
    raise ExpressionError(f"unknown value kind {kind!r}")


def parse_condition(raw: Any, _depth: int = 0, _budget: list[int] | None = None) -> Condition:
    budget = _budget if _budget is not None else [MAX_CONDITION_NODES]
    budget[0] -= 1
    if budget[0] < 0:
        raise ExpressionError(f"condition has more than {MAX_CONDITION_NODES} nodes")
    if _depth > MAX_CONDITION_DEPTH:
        raise ExpressionError(f"condition nested deeper than {MAX_CONDITION_DEPTH}")
    if not isinstance(raw, Mapping):
        raise ExpressionError("condition must be an object")
    if "all" in raw or "any" in raw:
        key = "all" if "all" in raw else "any"
        if set(raw) != {key} or not isinstance(raw[key], list) or not raw[key]:
            raise ExpressionError(f"'{key}' takes a non-empty list and nothing else")
        items = tuple(parse_condition(item, _depth + 1, budget) for item in raw[key])
        return AllOf(items) if key == "all" else AnyOf(items)
    if "not" in raw:
        if set(raw) != {"not"}:
            raise ExpressionError("'not' takes a single condition")
        return Not(parse_condition(raw["not"], _depth + 1, budget))
    op = raw.get("op")
    allowed = {"exists", "missing", "truthy", "equals", "not_equals", "in"}
    if op not in allowed:
        raise ExpressionError(f"condition op must be one of {sorted(allowed)}")
    expected_keys = {"op", "ref"} | ({"value"} if op in {"equals", "not_equals", "in"} else set())
    if set(raw) != expected_keys:
        raise ExpressionError(f"condition '{op}' takes exactly {sorted(expected_keys)}")
    value = raw.get("value")
    if op == "in" and (not isinstance(value, list) or not value):
        raise ExpressionError("'in' takes a non-empty list of constants")
    if isinstance(value, (Mapping, list)) and op != "in":
        raise ExpressionError("condition values must be scalar constants")
    return Compare(op=op, ref=parse_ref(raw["ref"]), value=value)


def refs_in_value(value: ValueRef) -> tuple[Ref, ...]:
    if isinstance(value, Ref):
        return (value,)
    if isinstance(value, Template):
        return tuple(part for part in value.parts if isinstance(part, Ref))
    if isinstance(value, FirstOf):
        return value.options
    return ()


def refs_in_condition(condition: Condition) -> tuple[Ref, ...]:
    if isinstance(condition, Compare):
        return (condition.ref,)
    if isinstance(condition, Not):
        return refs_in_condition(condition.item)
    return tuple(ref for item in condition.items for ref in refs_in_condition(item))


def resolve_ref(ref: Ref, scope: Mapping[str, Any]) -> Any:
    if ref.root == "trigger":
        current: Any = scope.get("trigger", MISSING)
    else:
        current = (scope.get("steps") or {}).get(ref.step_id, MISSING)
    for segment in ref.path:
        if current is MISSING:
            return MISSING
        if isinstance(segment, int):
            if isinstance(current, Sequence) and not isinstance(current, str) and segment < len(current):
                current = current[segment]
            else:
                return MISSING
        elif isinstance(current, Mapping) and segment in current:
            current = current[segment]
        else:
            return MISSING
    return current


def _as_text(value: Any) -> str:
    if value is MISSING or value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def resolve_value(value: ValueRef, scope: Mapping[str, Any]) -> Any:
    if isinstance(value, Const):
        return value.value
    if isinstance(value, Ref):
        return resolve_ref(value, scope)
    if isinstance(value, FirstOf):
        for option in value.options:
            found = resolve_ref(option, scope)
            if found is not MISSING and found is not None:
                return found
        return MISSING
    # Runtime text is substituted as inert data; templates never re-parse substituted content.
    return "".join(part if isinstance(part, str) else _as_text(resolve_ref(part, scope))
                   for part in value.parts)


def evaluate(condition: Condition, scope: Mapping[str, Any]) -> bool:
    if isinstance(condition, AllOf):
        return all(evaluate(item, scope) for item in condition.items)
    if isinstance(condition, AnyOf):
        return any(evaluate(item, scope) for item in condition.items)
    if isinstance(condition, Not):
        return not evaluate(condition.item, scope)
    found = resolve_ref(condition.ref, scope)
    match condition.op:
        case "exists":
            return found is not MISSING and found is not None
        case "missing":
            return found is MISSING or found is None
        case "truthy":
            return found is not MISSING and bool(found)
        case "equals":
            return found is not MISSING and found == condition.value
        case "not_equals":
            return found is MISSING or found != condition.value
        case "in":
            return found is not MISSING and found in condition.value
    raise ExpressionError(f"unknown op {condition.op!r}")


def value_to_wire(value: ValueRef) -> dict[str, Any]:
    if isinstance(value, Const):
        return {"const": value.value}
    if isinstance(value, Ref):
        return {"from": value.text()}
    if isinstance(value, FirstOf):
        return {"first_of": [option.text() for option in value.options]}
    text = "".join(part.replace("{", "{{").replace("}", "}}") if isinstance(part, str)
                   else "{" + part.text() + "}" for part in value.parts)
    return {"template": text}


def condition_to_wire(condition: Condition) -> dict[str, Any]:
    if isinstance(condition, AllOf):
        return {"all": [condition_to_wire(item) for item in condition.items]}
    if isinstance(condition, AnyOf):
        return {"any": [condition_to_wire(item) for item in condition.items]}
    if isinstance(condition, Not):
        return {"not": condition_to_wire(condition.item)}
    wire: dict[str, Any] = {"op": condition.op, "ref": condition.ref.text()}
    if condition.op in {"equals", "not_equals", "in"}:
        wire["value"] = condition.value
    return wire
