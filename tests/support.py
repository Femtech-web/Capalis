"""Test fixtures: the Capalis capability catalog plus negative-case capabilities, and the hero Recipe draft."""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

from capalis.adapters.catalog import CATALOG
from capalis.core.compiler import CompileResult, compile_recipe
from capalis.core.model import (
    Availability,
    CapabilityDescriptor,
    Effect,
    IdempotencySupport,
    TargetSpec,
    Trust,
)

REPO = "acme-labs/storefront"
SLACK = "C0ABCDEF12"
DISCORD = "123456789012345678"
REPORTER = "987654321098765432"
OWNER = "owner@example.test"

_STR = {"type": "string"}
_INT = {"type": "integer"}


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def capability(cid: str, *, effect: Effect, inputs: dict[str, Any], required: list[str],
               output: dict[str, Any] | None, target: TargetSpec | None, trust: Trust = Trust.REVIEWED,
               idempotency: IdempotencySupport = IdempotencySupport.NONE, reconcile_with: str | None = None,
               shell_like: bool = False, availability: Availability = Availability.AVAILABLE,
               version: str = "1.0.0", auth: str | None = None, operation: str | None = None,
               readback: tuple[str, ...] = ()) -> CapabilityDescriptor:
    source, _, tool = cid.partition(":")
    return CapabilityDescriptor(
        id=cid, source=source, provider=source, version=version, tool_name=tool, operation=operation or tool,
        summary=f"fixture {tool}", input_schema=_obj(inputs, required), output_schema=output, effect=effect,
        target=target, trust=trust, availability=availability, idempotency=idempotency,
        reconcile_with=reconcile_with, auth_requirement=auth, shell_like=shell_like,
        observed_at="2026-11-10T00:00:00Z", readback=readback,
    )


def registry() -> dict[str, CapabilityDescriptor]:
    """The real Capalis catalog (available), plus fixture-only capabilities for negative cases."""
    repo = TargetSpec("repo", "github.repo")
    caps = [replace(cap, availability=Availability.AVAILABLE) for cap in CATALOG] + [
        capability("capalis:github.issue.delete", effect=Effect.DELETE_OR_REVOKE, target=repo,
                   inputs={"repo": _STR, "number": _INT}, required=["repo", "number"],
                   output=_obj({"deleted": {"type": "boolean"}}, ["deleted"]), auth="CAPALIS_GITHUB_TOKEN"),
        capability("mcp:shell.exec", effect=Effect.EXTERNAL_WRITE, target=None,
                   inputs={"command": _STR}, required=["command"], output=None, shell_like=True),
        capability("mcp:notion.page.create", effect=Effect.EXTERNAL_WRITE, trust=Trust.UNVERIFIED,
                   target=TargetSpec("workspace", "notion.workspace"),
                   inputs={"workspace": _STR, "title": _STR}, required=["workspace", "title"],
                   output=_obj({"id": _STR}, ["id"])),
    ]
    return {cap.id: cap for cap in caps}


def hero_draft() -> dict[str, Any]:
    return {
        "intent": "Track Discord bug reports as GitHub issues and tell engineering in Slack",
        "trigger": {
            "kind": "event",
            "source": f"discord:channel:{DISCORD}",
            "senders": [REPORTER],
            "payload_schema": _obj({"message_id": _STR, "author": _STR,
                                    "text": {"type": "string", "maxLength": 4000}},
                                   ["message_id", "author", "text"]),
            "dedupe_key": "message_id",
            "max_runs_per_hour": 20,
        },
        "steps": [
            {"id": "find", "capability": "capalis:github.issue.search",
             "inputs": {"repo": {"const": REPO}, "query": {"from": "trigger.message_id"}}},
            {"id": "create", "capability": "capalis:github.issue.create",
             "when": {"op": "equals", "ref": "steps.find.total_count", "value": 0},
             "inputs": {"repo": {"const": REPO},
                        "title": {"template": "Bug report from {trigger.author}"},
                        "body": {"template": "{trigger.text}\n\nDiscord message {trigger.message_id}"}},
             "idempotency": "reconcile",
             "verify": [{"id": "issue_readback", "probe": "capalis:github.issue.get",
                         "inputs": {"repo": {"const": REPO}, "number": {"from": "steps.create.number"}},
                         "expect": [{"path": "state", "op": "equals", "value": {"const": "open"}},
                                    {"path": "body", "op": "contains", "value": {"from": "trigger.message_id"}}],
                         "level": "V1", "deadline_s": 120}]},
            {"id": "notify", "capability": "capalis:slack.message.post", "after": ["create"],
             "inputs": {"channel": {"const": SLACK},
                        "text": {"template": "New bug report: {steps.create.html_url}{steps.find.items.0.html_url}"}},
             "verify": [{"id": "message_readback", "probe": "capalis:slack.message.get",
                         "inputs": {"channel": {"const": SLACK}, "ts": {"from": "steps.notify.ts"}},
                         "expect": [{"path": "ts", "op": "equals", "value": {"from": "steps.notify.ts"}},
                                    {"path": "text", "op": "contains", "value": {"const": "New bug report"}}],
                         "level": "V1"}]},
        ],
        "budgets": {"max_writes_per_run": 4, "max_runs_per_day": 100},
    }


def with_deploy_wait(draft: dict[str, Any]) -> dict[str, Any]:
    draft["steps"].append({
        "id": "deployed", "kind": "wait", "capability": "capalis:http.get", "after": ["notify"],
        "inputs": {"origin": {"const": "https://shop.example.com"}, "path": {"const": "/version"}},
        "wait": {"until": {"op": "equals", "ref": "steps.deployed.status", "value": 200},
                 "poll_interval_s": 300, "deadline_s": 7 * 24 * 3600},
    })
    return draft


def manual_trigger(draft: dict[str, Any], **trigger: Any) -> dict[str, Any]:
    draft["trigger"] = {"kind": "manual", **trigger}
    for step in draft["steps"]:
        for key in ("inputs",):
            for name, value in list(step.get(key, {}).items()):
                if "trigger." in str(value):
                    step[key][name] = {"const": "manual run"}
        for contract in step.get("verify", []):
            contract["expect"] = [e for e in contract["expect"] if "trigger." not in str(e)]
    return draft


def compile_draft(draft: dict[str, Any], *, caps: dict[str, CapabilityDescriptor] | None = None,
                  version: int = 1) -> CompileResult:
    return compile_recipe(copy.deepcopy(draft), caps if caps is not None else registry(),
                          recipe_id="bug-reports", version=version, owner=OWNER, workspace="acme")


def codes(result: CompileResult) -> set[str]:
    return {problem.code for problem in result.problems}
