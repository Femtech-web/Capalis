"""Decode a stored Recipe back into a RecipeSpec, exactly as it was compiled.

Stored Recipes are reloaded from their own wire form, never recompiled against the current registry:
recompiling would silently adopt whatever the capabilities look like now, which is precisely the drift
the pins exist to detect. The digest of the decoded Recipe must equal the stored digest.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .expressions import parse_condition, parse_value
from .model import (
    ApprovalGrant,
    ApprovalPolicy,
    Budgets,
    DependencyPin,
    Effect,
    EvidenceLevel,
    Expectation,
    IdempotencyStrategy,
    OutcomeContract,
    RecipeSpec,
    RetryPolicy,
    StepKind,
    StepSpec,
    Target,
    TriggerKind,
    TriggerPolicy,
    Trust,
    WaitPolicy,
)


class WireError(ValueError):
    pass


def _path(text: str | None) -> tuple[str | int, ...] | None:
    if text is None:
        return None
    return tuple(int(part) if part.isdigit() else part for part in text.split("."))


def _target(raw: Mapping[str, str] | None) -> Target | None:
    return None if raw is None else Target(raw["kind"], raw["value"])


def _contract(raw: Mapping[str, Any]) -> OutcomeContract:
    expect = tuple(
        Expectation(_path(item["path"]) or (), item["op"],
                    parse_value(item["value"]) if "value" in item else None)
        for item in raw["expect"]
    )
    return OutcomeContract(
        contract_id=raw["id"], probe=raw["probe"],
        inputs={name: parse_value(value) for name, value in raw["inputs"].items()}, expect=expect,
        level=EvidenceLevel(raw["level"]), probe_target=_target(raw["probe_target"]),
        deadline_s=raw["deadline_s"], freshness_s=raw["freshness_s"], required=raw["required"],
    )


def _step(raw: Mapping[str, Any]) -> StepSpec:
    wait = raw.get("wait")
    return StepSpec(
        step_id=raw["id"], kind=StepKind(raw["kind"]), capability_id=raw["capability"],
        effect=Effect(raw["effect"]), target=_target(raw["target"]),
        inputs={name: parse_value(value) for name, value in raw["inputs"].items()},
        depends_on=tuple(raw["depends_on"]),
        when=parse_condition(raw["when"]) if raw["when"] is not None else None,
        idempotency=IdempotencyStrategy(raw["idempotency"]),
        retry=RetryPolicy(raw["retry"]["max_attempts"], raw["retry"]["backoff_s"]), timeout_s=raw["timeout_s"],
        verify=tuple(_contract(c) for c in raw["verify"]), unverifiable_reason=raw["unverifiable_reason"],
        wait=None if wait is None else WaitPolicy(parse_condition(wait["until"]), wait["poll_interval_s"],
                                                  wait["deadline_s"]),
    )


def recipe_from_wire(wire: Mapping[str, Any], expected_digest: str | None = None) -> RecipeSpec:
    try:
        trigger = wire["trigger"]
        recipe = RecipeSpec(
            recipe_id=wire["recipe_id"], version=wire["version"], owner=wire["owner"], workspace=wire["workspace"],
            intent=wire["intent"],
            trigger=TriggerPolicy(
                kind=TriggerKind(trigger["kind"]), source=trigger["source"], senders=frozenset(trigger["senders"]),
                payload_schema=trigger["payload_schema"], dedupe_key=_path(trigger["dedupe_key"]),
                max_runs_per_hour=trigger["max_runs_per_hour"], schedule=trigger["schedule"],
            ),
            steps=tuple(_step(step) for step in wire["steps"]),
            budgets=Budgets(**wire["budgets"]),
            approval=ApprovalPolicy(frozenset(Effect(e) for e in wire["approval"]["per_run_confirmation"]),
                                    wire["approval"]["grant_ttl_s"]),
            retention_days=wire["retention_days"],
            pins={key: DependencyPin(capability_id=pin["capability_id"], provider=pin["provider"],
                                     version=pin["version"], schema_fingerprint=pin["schema_fingerprint"],
                                     trust=Trust(pin["trust"]), auth_requirement=pin["auth_requirement"])
                  for key, pin in wire["pins"].items()},
            previous_digest=wire["previous_digest"], schema_version=wire["schema_version"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WireError(f"stored Recipe is malformed: {exc}") from exc
    if expected_digest is not None and recipe.digest != expected_digest:
        raise WireError("stored Recipe does not match its recorded digest")
    return recipe


def grant_from_wire(wire: Mapping[str, Any]) -> ApprovalGrant:
    try:
        return ApprovalGrant(
            grant_id=wire["grant_id"], recipe_id=wire["recipe_id"], recipe_digest=wire["recipe_digest"],
            actor=wire["actor"], effects=frozenset(Effect(e) for e in wire["effects"]),
            targets=frozenset(Target(t["kind"], t["value"]) for t in wire["targets"]),
            trigger_sources=frozenset(wire["trigger_sources"]), spend_limit_cents=wire["spend_limit_cents"],
            issued_at=wire["issued_at"], expires_at=wire["expires_at"], revoked_at=wire["revoked_at"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WireError(f"stored grant is malformed: {exc}") from exc
