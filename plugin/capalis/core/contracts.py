"""Capability contracts: how another Hermes tool (built-in or from another plugin) becomes usable in Recipes.

A contract states what a tool takes, what it returns and that it only reads. It is untrusted until the
owner has seen a passing contract test and approved it (FR-REG-05, D-014): only then does its effect count.
Contracts that change things are refused for now, because a write needs a sandbox test and a read-back the
owner can rely on.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import schema as jsonschema
from .canonical import digest
from .model import Availability, CapabilityDescriptor, Effect, TargetSpec, Trust

TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{1,63}")
PLUGIN_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}")
OPERATION = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
_KEYS = frozenset({"tool", "plugin", "summary", "operation", "effect", "input_schema", "output_schema",
                   "output_format", "target", "sample_inputs"})
TEXT_OUTPUT = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
               "additionalProperties": False}


@dataclass(frozen=True, slots=True)
class ContractSpec:
    tool: str
    plugin: str | None
    summary: str
    operation: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    output_format: str
    target: TargetSpec | None
    sample_inputs: Mapping[str, Any]

    @property
    def capability_id(self) -> str:
        return f"hermes:{self.tool}"

    def to_wire(self) -> dict[str, Any]:
        return {"tool": self.tool, "plugin": self.plugin, "summary": self.summary, "operation": self.operation,
                "effect": Effect.READ.value, "input_schema": self.input_schema, "output_schema": self.output_schema,
                "output_format": self.output_format,
                "target": None if self.target is None else {"param": self.target.param, "kind": self.target.kind},
                "sample_inputs": self.sample_inputs}

    @property
    def digest(self) -> str:
        return digest(self.to_wire())

    def descriptor(self, trust: Trust, availability: Availability) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            id=self.capability_id, source="hermes", provider=self.plugin or "hermes", version=self.digest[7:19],
            tool_name=self.tool, operation=self.operation, summary=self.summary, input_schema=self.input_schema,
            output_schema=self.output_schema, effect=Effect.READ, target=self.target, trust=trust,
            availability=availability, limits=("a Hermes tool described by an owner-approved contract",))


def _schema_problems(name: str, value: Any) -> list[str]:
    if not isinstance(value, Mapping) or jsonschema.declared_types(value) != frozenset({"object"}):
        return [f"{name} must be a JSON Schema object"]
    unsupported = jsonschema.unsupported_keywords(value)
    return [f"{name} uses unsupported keywords: {', '.join(unsupported[:3])}"] if unsupported else []


def parse_contract(raw: Any) -> tuple[ContractSpec | None, list[str]]:
    if not isinstance(raw, Mapping):
        return None, ["a contract is an object"]
    problems = [f"unknown field {key!r}" for key in sorted(set(raw) - _KEYS)]
    tool, plugin = raw.get("tool"), raw.get("plugin")
    if not isinstance(tool, str) or not TOOL_NAME.fullmatch(tool):
        problems.append("tool must be the Hermes tool name, e.g. read_file")
    if plugin is not None and (not isinstance(plugin, str) or not PLUGIN_NAME.fullmatch(plugin)):
        problems.append("plugin must be the providing plugin's name, or omitted for built-in tools")
    if raw.get("effect", "read") != "read":
        problems.append("only tools that read can be contracted for now; a tool that changes things needs a "
                        "sandbox contract test and an independent read-back first")
    operation = raw.get("operation")
    if not isinstance(operation, str) or not OPERATION.fullmatch(operation):
        problems.append("operation must be a dotted name such as file.read or web.read")
    summary = raw.get("summary")
    if not isinstance(summary, str) or not 5 <= len(summary) <= 200:
        problems.append("summary must say in 5-200 characters what the tool reads")
    problems += _schema_problems("input_schema", raw.get("input_schema"))
    output_format = raw.get("output_format", "json")
    if output_format not in ("json", "text"):
        problems.append("output_format is json or text")
    output_schema = TEXT_OUTPUT if output_format == "text" else raw.get("output_schema")
    if output_format == "json":
        problems += _schema_problems("output_schema", output_schema)
    target = None
    if raw.get("target") is not None:
        t = raw["target"]
        if (not isinstance(t, Mapping) or not isinstance(t.get("param"), str) or not isinstance(t.get("kind"), str)
                or t["param"] not in ((raw.get("input_schema") or {}).get("properties") or {})):
            problems.append("target needs param (an input name) and kind")
        else:
            target = TargetSpec(t["param"], t["kind"])
    samples = raw.get("sample_inputs")
    if not isinstance(samples, Mapping):
        problems.append("sample_inputs are needed so the owner can run a contract test")
    elif isinstance(raw.get("input_schema"), Mapping):
        problems += [f"sample_inputs: {p}" for p in jsonschema.validate(dict(samples), raw["input_schema"])]
    if problems:
        return None, problems
    return ContractSpec(tool=str(tool), plugin=plugin, summary=str(summary), operation=str(operation),
                        input_schema=dict(raw["input_schema"]), output_schema=dict(output_schema or {}),
                        output_format=str(output_format), target=target, sample_inputs=dict(samples or {})), []
