from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .canonical import digest
from .expressions import Condition, ValueRef, condition_to_wire, value_to_wire

RECIPE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class Problem:
    code: str
    path: str
    message: str


class Effect(StrEnum):
    READ = "read"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    EXTERNAL_SEND = "external_send"
    SPEND = "spend"
    DELETE_OR_REVOKE = "delete_or_revoke"
    UNKNOWN = "unknown"


EFFECT_RISK: Mapping[Effect, int] = {
    Effect.READ: 0,
    Effect.LOCAL_WRITE: 1,
    Effect.EXTERNAL_WRITE: 2,
    Effect.EXTERNAL_SEND: 3,
    Effect.SPEND: 4,
    Effect.DELETE_OR_REVOKE: 5,
    Effect.UNKNOWN: 6,
}

EXTERNAL_EFFECTS = frozenset({
    Effect.EXTERNAL_WRITE, Effect.EXTERNAL_SEND, Effect.SPEND, Effect.DELETE_OR_REVOKE, Effect.UNKNOWN,
})


class Trust(StrEnum):
    UNVERIFIED = "unverified"
    DECLARED = "declared"
    TESTED = "tested"
    REVIEWED = "reviewed"


TRUSTED_LEVELS = frozenset({Trust.TESTED, Trust.REVIEWED})


class Availability(StrEnum):
    AVAILABLE = "available"
    NOT_INSTALLED = "not_installed"
    DISABLED = "disabled"
    NEEDS_AUTH = "needs_auth"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


class IdempotencySupport(StrEnum):
    NOT_NEEDED = "not_needed"
    NATIVE_KEY = "native_key"
    RECONCILE = "reconcile"
    NONE = "none"


class IdempotencyStrategy(StrEnum):
    NOT_NEEDED = "not_needed"
    NATIVE_KEY = "native_key"
    RECONCILE = "reconcile"
    NONE = "none"


class EvidenceLevel(StrEnum):
    V0 = "V0"
    V1 = "V1"
    V2 = "V2"


EVIDENCE_RANK: Mapping[EvidenceLevel, int] = {EvidenceLevel.V0: 0, EvidenceLevel.V1: 1, EvidenceLevel.V2: 2}


class TriggerKind(StrEnum):
    MANUAL = "manual"
    EVENT = "event"
    SCHEDULE = "schedule"


class StepKind(StrEnum):
    ACTION = "action"
    WAIT = "wait"


_TARGET_PATTERNS: Mapping[str, re.Pattern[str]] = {
    "github.repo": re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}"),
    "slack.channel": re.compile(r"[CGD][A-Z0-9]{8,12}"),
    "discord.channel": re.compile(r"[0-9]{17,20}"),
    "http.origin": re.compile(r"https://[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{2,5})?"),
    "hermes.chat": re.compile(r"[a-z][a-z0-9_]{1,31}:[A-Za-z0-9_.:@-]{1,128}"),
    "telegram.chat": re.compile(r"-?[0-9]{5,20}|@[A-Za-z][A-Za-z0-9_]{4,31}"),
}
_GENERIC_TARGET = re.compile(r"[A-Za-z0-9._:/@-]{1,200}")
_KIND = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")


@dataclass(frozen=True, slots=True, order=True)
class Target:
    kind: str
    value: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"

    def to_wire(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value}


def target_problem(kind: str, value: Any) -> str | None:
    if not _KIND.fullmatch(kind):
        return f"target kind {kind!r} is not a dotted lowercase name"
    if not isinstance(value, str):
        return f"{kind} target must be a string"
    pattern = _TARGET_PATTERNS.get(kind, _GENERIC_TARGET)
    if not pattern.fullmatch(value):
        return f"{value!r} is not a valid {kind} target"
    return None


@dataclass(frozen=True, slots=True)
class TargetSpec:
    """The input parameter that selects where a capability's effect lands, and its target kind."""

    param: str
    kind: str


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    id: str
    source: str
    provider: str
    version: str
    tool_name: str
    operation: str
    summary: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any] | None
    effect: Effect
    target: TargetSpec | None
    trust: Trust
    availability: Availability
    idempotency: IdempotencySupport = IdempotencySupport.NONE
    reconcile_with: str | None = None
    auth_requirement: str | None = None
    shell_like: bool = False
    observed_at: str = ""
    limits: tuple[str, ...] = ()
    readback: tuple[str, ...] = ()
    setup_hint: str = ""

    @property
    def effective_effect(self) -> Effect:
        # FR-REG-04: a self-reported effect only counts once Capalis holds trusted evidence for it.
        return self.effect if self.trust in TRUSTED_LEVELS else Effect.UNKNOWN

    @property
    def schema_fingerprint(self) -> str:
        return digest({
            "tool_name": self.tool_name,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
        })

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id, "source": self.source, "provider": self.provider, "version": self.version,
            "tool_name": self.tool_name, "operation": self.operation, "summary": self.summary,
            "input_schema": self.input_schema, "output_schema": self.output_schema,
            "effect": self.effect.value,
            "target": None if self.target is None else {"param": self.target.param, "kind": self.target.kind},
            "trust": self.trust.value, "availability": self.availability.value,
            "idempotency": self.idempotency.value, "reconcile_with": self.reconcile_with,
            "auth_requirement": self.auth_requirement, "shell_like": self.shell_like,
            "observed_at": self.observed_at, "limits": list(self.limits), "readback": list(self.readback),
            "setup_hint": self.setup_hint, "schema_fingerprint": self.schema_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class DependencyPin:
    capability_id: str
    provider: str
    version: str
    schema_fingerprint: str
    trust: Trust
    auth_requirement: str | None

    @classmethod
    def of(cls, capability: CapabilityDescriptor) -> DependencyPin:
        return cls(capability_id=capability.id, provider=capability.provider, version=capability.version,
                   schema_fingerprint=capability.schema_fingerprint, trust=capability.trust,
                   auth_requirement=capability.auth_requirement)

    def to_wire(self) -> dict[str, Any]:
        return {"capability_id": self.capability_id, "provider": self.provider, "version": self.version,
                "schema_fingerprint": self.schema_fingerprint, "trust": self.trust.value,
                "auth_requirement": self.auth_requirement}


@dataclass(frozen=True, slots=True)
class Expectation:
    path: tuple[str | int, ...]
    op: str
    value: ValueRef | None = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"path": ".".join(str(p) for p in self.path), "op": self.op}
        if self.value is not None:
            wire["value"] = value_to_wire(self.value)
        return wire


@dataclass(frozen=True, slots=True)
class OutcomeContract:
    contract_id: str
    probe: str
    inputs: Mapping[str, ValueRef]
    expect: tuple[Expectation, ...]
    level: EvidenceLevel
    probe_target: Target | None
    deadline_s: int
    freshness_s: int
    required: bool = True

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.contract_id, "probe": self.probe,
            "inputs": {name: value_to_wire(value) for name, value in sorted(self.inputs.items())},
            "expect": [item.to_wire() for item in self.expect], "level": self.level.value,
            "probe_target": None if self.probe_target is None else self.probe_target.to_wire(),
            "deadline_s": self.deadline_s, "freshness_s": self.freshness_s, "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 1
    backoff_s: int = 0

    def to_wire(self) -> dict[str, int]:
        return {"max_attempts": self.max_attempts, "backoff_s": self.backoff_s}


@dataclass(frozen=True, slots=True)
class WaitPolicy:
    until: Condition
    poll_interval_s: int
    deadline_s: int

    def to_wire(self) -> dict[str, Any]:
        return {"until": condition_to_wire(self.until), "poll_interval_s": self.poll_interval_s,
                "deadline_s": self.deadline_s}


@dataclass(frozen=True, slots=True)
class StepSpec:
    step_id: str
    kind: StepKind
    capability_id: str
    effect: Effect
    target: Target | None
    inputs: Mapping[str, ValueRef]
    depends_on: tuple[str, ...]
    when: Condition | None
    idempotency: IdempotencyStrategy
    retry: RetryPolicy
    timeout_s: int
    verify: tuple[OutcomeContract, ...]
    unverifiable_reason: str | None
    wait: WaitPolicy | None = None

    @property
    def is_side_effect(self) -> bool:
        return self.effect is not Effect.READ

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.step_id, "kind": self.kind.value, "capability": self.capability_id,
            "effect": self.effect.value, "target": None if self.target is None else self.target.to_wire(),
            "inputs": {name: value_to_wire(value) for name, value in sorted(self.inputs.items())},
            "depends_on": list(self.depends_on),
            "when": None if self.when is None else condition_to_wire(self.when),
            "idempotency": self.idempotency.value, "retry": self.retry.to_wire(),
            "timeout_s": self.timeout_s, "verify": [contract.to_wire() for contract in self.verify],
            "unverifiable_reason": self.unverifiable_reason,
            "wait": None if self.wait is None else self.wait.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class TriggerPolicy:
    kind: TriggerKind
    source: str
    senders: frozenset[str]
    payload_schema: Mapping[str, Any]
    dedupe_key: tuple[str | int, ...] | None
    max_runs_per_hour: int
    schedule: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value, "source": self.source, "senders": sorted(self.senders),
            "payload_schema": self.payload_schema,
            "dedupe_key": None if self.dedupe_key is None else ".".join(str(p) for p in self.dedupe_key),
            "max_runs_per_hour": self.max_runs_per_hour, "schedule": self.schedule,
        }


@dataclass(frozen=True, slots=True)
class Budgets:
    max_writes_per_run: int
    max_runs_per_day: int
    spend_limit_cents: int = 0

    def to_wire(self) -> dict[str, int]:
        return {"max_writes_per_run": self.max_writes_per_run, "max_runs_per_day": self.max_runs_per_day,
                "spend_limit_cents": self.spend_limit_cents}


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    per_run_confirmation: frozenset[Effect]
    grant_ttl_s: int

    def to_wire(self) -> dict[str, Any]:
        return {"per_run_confirmation": sorted(effect.value for effect in self.per_run_confirmation),
                "grant_ttl_s": self.grant_ttl_s}


@dataclass(frozen=True, slots=True)
class RecipeSpec:
    recipe_id: str
    version: int
    owner: str
    workspace: str
    intent: str
    trigger: TriggerPolicy
    steps: tuple[StepSpec, ...]
    budgets: Budgets
    approval: ApprovalPolicy
    retention_days: int
    pins: Mapping[str, DependencyPin]
    previous_digest: str | None = None
    schema_version: int = RECIPE_SCHEMA_VERSION
    _digest: str = field(default="", compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_digest", digest(self.to_wire()))

    @property
    def digest(self) -> str:
        return self._digest

    def step(self, step_id: str) -> StepSpec | None:
        return next((step for step in self.steps if step.step_id == step_id), None)

    @property
    def effects(self) -> frozenset[Effect]:
        found = {step.effect for step in self.steps}
        found.update(Effect.READ for step in self.steps
                     if step.verify or step.idempotency is IdempotencyStrategy.RECONCILE)
        return frozenset(found)

    @property
    def targets(self) -> frozenset[Target]:
        found = {step.target for step in self.steps if step.target is not None}
        found.update(contract.probe_target for step in self.steps for contract in step.verify
                     if contract.probe_target is not None)
        return frozenset(found)

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "recipe_id": self.recipe_id, "version": self.version,
            "owner": self.owner, "workspace": self.workspace, "intent": self.intent,
            "trigger": self.trigger.to_wire(), "steps": [step.to_wire() for step in self.steps],
            "budgets": self.budgets.to_wire(), "approval": self.approval.to_wire(),
            "retention_days": self.retention_days,
            "pins": {key: pin.to_wire() for key, pin in sorted(self.pins.items())},
            "previous_digest": self.previous_digest,
        }


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    grant_id: str
    recipe_id: str
    recipe_digest: str
    actor: str
    effects: frozenset[Effect]
    targets: frozenset[Target]
    trigger_sources: frozenset[str]
    spend_limit_cents: int
    issued_at: int
    expires_at: int
    revoked_at: int | None = None

    def active_at(self, now: int) -> bool:
        return self.revoked_at is None and self.issued_at <= now < self.expires_at

    def to_wire(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id, "recipe_id": self.recipe_id, "recipe_digest": self.recipe_digest,
            "actor": self.actor, "effects": sorted(effect.value for effect in self.effects),
            "targets": [target.to_wire() for target in sorted(self.targets)],
            "trigger_sources": sorted(self.trigger_sources), "spend_limit_cents": self.spend_limit_cents,
            "issued_at": self.issued_at, "expires_at": self.expires_at, "revoked_at": self.revoked_at,
        }


def grant_for(recipe: RecipeSpec, *, grant_id: str, actor: str, now: int,
              ttl_s: int | None = None) -> ApprovalGrant:
    """A grant covering exactly this Recipe version's effects, targets and trigger source."""
    return ApprovalGrant(
        grant_id=grant_id, recipe_id=recipe.recipe_id, recipe_digest=recipe.digest, actor=actor,
        effects=recipe.effects, targets=recipe.targets, trigger_sources=frozenset({recipe.trigger.source}),
        spend_limit_cents=recipe.budgets.spend_limit_cents, issued_at=now,
        expires_at=now + (ttl_s if ttl_s is not None else recipe.approval.grant_ttl_s),
    )
