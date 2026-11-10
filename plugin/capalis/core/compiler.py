"""Compile an untrusted draft (typically model-written) into an immutable RecipeSpec.

The draft is data. Nothing in it is executed, and anything the compiler cannot prove safe or
well-typed is reported as a Problem rather than repaired silently.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import schema as jsonschema
from .expressions import (
    Condition,
    Const,
    ExpressionError,
    FirstOf,
    Ref,
    Template,
    ValueRef,
    parse_condition,
    parse_value,
    refs_in_condition,
    refs_in_value,
)
from .model import (
    EXTERNAL_EFFECTS,
    ApprovalPolicy,
    Availability,
    Budgets,
    CapabilityDescriptor,
    DependencyPin,
    Effect,
    EvidenceLevel,
    Expectation,
    IdempotencyStrategy,
    IdempotencySupport,
    OutcomeContract,
    Problem,
    RecipeSpec,
    RetryPolicy,
    StepKind,
    StepSpec,
    Target,
    TriggerKind,
    TriggerPolicy,
    WaitPolicy,
    target_problem,
)

MAX_STEPS = 32
MAX_CONTRACTS_PER_STEP = 4
MAX_EXPECTATIONS = 8
MAX_INTENT_LENGTH = 500
THIRTY_DAYS = 30 * 24 * 3600
NINETY_DAYS = 90 * 24 * 3600

_STEP_ID = re.compile(r"[a-z][a-z0-9_]{0,31}")
_RECIPE_ID = re.compile(r"[a-z0-9][a-z0-9-]{2,63}")
_SOURCE = re.compile(r"[a-z][a-z0-9_]{1,31}:[A-Za-z0-9_.:@/-]{1,200}")
_CRON_FIELD = re.compile(r"(\*|[0-9]{1,2}(-[0-9]{1,2})?)(/[0-9]{1,2})?(,(\*|[0-9]{1,2}(-[0-9]{1,2})?)(/[0-9]{1,2})?)*")
_PATH_SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|0|[1-9][0-9]{0,5}")

_TOP_KEYS = frozenset({"intent", "trigger", "steps", "budgets", "approval", "retention_days"})
_TRIGGER_KEYS = frozenset({"kind", "source", "senders", "payload_schema", "dedupe_key",
                           "max_runs_per_hour", "schedule"})
_STEP_KEYS = frozenset({"id", "kind", "capability", "inputs", "when", "after", "effect", "idempotency",
                        "retry", "timeout_s", "verify", "unverifiable_reason", "wait"})
_CONTRACT_KEYS = frozenset({"id", "probe", "inputs", "expect", "level", "deadline_s", "freshness_s",
                            "required"})
_EXPECT_OPS = frozenset({"equals", "not_equals", "contains", "exists", "in"})


@dataclass(frozen=True, slots=True)
class SetupItem:
    capability_id: str
    availability: Availability
    auth_requirement: str | None


@dataclass(frozen=True, slots=True)
class CompileResult:
    recipe: RecipeSpec | None
    problems: tuple[Problem, ...]
    setup_needed: tuple[SetupItem, ...]

    @property
    def ok(self) -> bool:
        return self.recipe is not None and not self.problems


def _reconcile_output_fits(lookup: jsonschema.Schema | None, write: jsonschema.Schema | None) -> bool:
    properties = (lookup or {}).get("properties") or {}
    found = properties.get("found")
    return (isinstance(found, Mapping) and jsonschema.declared_types(found) == frozenset({"boolean"})
            and "found" in ((lookup or {}).get("required") or ()) and write is not None
            and jsonschema.assignable(properties.get("resource"), write))


def compile_recipe(
    draft: Any,
    capabilities: Mapping[str, CapabilityDescriptor],
    *,
    recipe_id: str,
    version: int,
    owner: str,
    workspace: str,
    previous_digest: str | None = None,
) -> CompileResult:
    return _Compiler(capabilities, owner).run(draft, recipe_id=recipe_id, version=version,
                                              workspace=workspace, previous_digest=previous_digest)


def _parse_path(text: Any) -> tuple[str | int, ...] | None:
    if not isinstance(text, str) or not text:
        return None
    segments = text.split(".")
    if len(segments) > 8 or not all(_PATH_SEGMENT.fullmatch(s) for s in segments):
        return None
    return tuple(int(s) if s.isdigit() else s for s in segments)


def _bounded_int(raw: Any, default: int, low: int, high: int) -> int | None:
    value = default if raw is None else raw
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return None
    return value


class _Compiler:
    def __init__(self, capabilities: Mapping[str, CapabilityDescriptor], owner: str) -> None:
        self.capabilities = capabilities
        self.owner = owner
        self.problems: list[Problem] = []
        self.used: dict[str, CapabilityDescriptor] = {}
        self.trigger_schema: Mapping[str, Any] = {}
        self.declared_steps: dict[str, Mapping[str, Any]] = {}

    def error(self, code: str, path: str, message: str) -> None:
        self.problems.append(Problem(code, path, message))

    def run(self, draft: Any, *, recipe_id: str, version: int, workspace: str,
            previous_digest: str | None) -> CompileResult:
        if not isinstance(draft, Mapping):
            self.error("E_DRAFT_TYPE", "$", "the draft must be an object")
            return self._result(None)
        if not _RECIPE_ID.fullmatch(recipe_id):
            self.error("E_RECIPE_ID", "recipe_id", "use 3-64 lowercase letters, digits or dashes")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            self.error("E_VERSION", "version", "version must be a positive integer")
        self._reject_unknown(draft, _TOP_KEYS, "$")

        intent = draft.get("intent")
        if not isinstance(intent, str) or not intent.strip() or len(intent) > MAX_INTENT_LENGTH:
            self.error("E_INTENT", "$.intent", f"describe the goal in 1-{MAX_INTENT_LENGTH} characters")
            intent = ""

        trigger = self._trigger(draft.get("trigger"))
        steps = self._steps(draft.get("steps"))
        budgets = self._budgets(draft.get("budgets"), steps)
        approval = self._approval(draft.get("approval"), steps)
        retention = _bounded_int(draft.get("retention_days"), 30, 1, 365)
        if retention is None:
            self.error("E_RETENTION", "$.retention_days", "retention must be 1-365 days")

        if self.problems or trigger is None or budgets is None or approval is None or retention is None:
            return self._result(None)
        recipe = RecipeSpec(
            recipe_id=recipe_id, version=version, owner=self.owner, workspace=workspace,
            intent=intent.strip(), trigger=trigger, steps=steps, budgets=budgets, approval=approval,
            retention_days=retention,
            pins={cid: DependencyPin.of(cap) for cid, cap in sorted(self.used.items())},
            previous_digest=previous_digest,
        )
        return self._result(recipe)

    def _result(self, recipe: RecipeSpec | None) -> CompileResult:
        setup = tuple(
            SetupItem(cap.id, cap.availability, cap.auth_requirement)
            for cap in sorted(self.used.values(), key=lambda c: c.id)
            if cap.availability is not Availability.AVAILABLE
        )
        return CompileResult(recipe if not self.problems else None, tuple(self.problems), setup)

    def _reject_unknown(self, raw: Mapping[str, Any], allowed: frozenset[str], path: str) -> None:
        for key in sorted(set(raw) - allowed):
            self.error("E_UNKNOWN_FIELD", f"{path}.{key}", "field is not part of the Recipe format")

    def _capability(self, capability_id: Any, path: str) -> CapabilityDescriptor | None:
        cap = self.capabilities.get(capability_id) if isinstance(capability_id, str) else None
        if cap is None:
            self.error("E_UNKNOWN_CAPABILITY", path, f"{capability_id!r} is not a known capability")
            return None
        unsupported = jsonschema.unsupported_keywords(cap.input_schema)
        if cap.output_schema is not None:
            unsupported += jsonschema.unsupported_keywords(cap.output_schema)
        if unsupported:
            self.error("E_SCHEMA_UNSUPPORTED", path,
                       f"{cap.id} uses schema features Capalis cannot type-check: {unsupported[:3]}")
        self.used[cap.id] = cap
        return cap

    def _trigger(self, raw: Any) -> TriggerPolicy | None:
        path = "$.trigger"
        if not isinstance(raw, Mapping):
            self.error("E_TRIGGER", path, "a trigger is required")
            return None
        self._reject_unknown(raw, _TRIGGER_KEYS, path)
        kind_raw = raw.get("kind")
        if kind_raw not in {kind.value for kind in TriggerKind}:
            self.error("E_TRIGGER", f"{path}.kind", "kind must be manual, event or schedule")
            return None
        kind = TriggerKind(kind_raw)

        payload_schema = raw.get("payload_schema", {"type": "object", "properties": {},
                                                    "additionalProperties": False})
        if (not isinstance(payload_schema, Mapping)
                or jsonschema.declared_types(payload_schema) != frozenset({"object"})):
            self.error("E_PAYLOAD_SCHEMA", f"{path}.payload_schema", "the payload schema must describe an object")
            return None
        unsupported = jsonschema.unsupported_keywords(payload_schema)
        if unsupported:
            self.error("E_SCHEMA_UNSUPPORTED", f"{path}.payload_schema", f"unsupported keywords: {unsupported[:3]}")
        if payload_schema.get("additionalProperties", True) is not False:
            self.error("E_PAYLOAD_SCHEMA", f"{path}.payload_schema",
                       "set additionalProperties to false so unexpected fields are rejected")
        self.trigger_schema = payload_schema

        rate = _bounded_int(raw.get("max_runs_per_hour"), 10, 1, 120)
        if rate is None:
            self.error("E_TRIGGER_RATE", f"{path}.max_runs_per_hour", "rate must be 1-120 runs per hour")
            rate = 1

        senders_raw = raw.get("senders")
        dedupe = None
        schedule = None
        if kind is TriggerKind.EVENT:
            source = raw.get("source")
            if not isinstance(source, str) or not _SOURCE.fullmatch(source):
                self.error("E_TRIGGER_SOURCE", f"{path}.source", "name the exact origin, e.g. discord:channel:<id>")
                source = ""
            if (not isinstance(senders_raw, list) or not senders_raw
                    or not all(isinstance(s, str) and s and s != "*" for s in senders_raw)):
                self.error("E_TRIGGER_UNSCOPED", f"{path}.senders",
                           "event triggers need an explicit list of authorized sender ids")
                senders: frozenset[str] = frozenset()
            else:
                senders = frozenset(senders_raw)
            dedupe = _parse_path(raw.get("dedupe_key"))
            dedupe_schema = jsonschema.subschema_at(payload_schema, dedupe) if dedupe else None
            required = set(payload_schema.get("required") or ())
            if (dedupe is None or dedupe_schema is None or not isinstance(dedupe[0], str) or dedupe[0] not in required
                    or not (jsonschema.declared_types(dedupe_schema) or set()) <= {"string", "integer"}):
                self.error("E_TRIGGER_NO_DEDUPE", f"{path}.dedupe_key",
                           "event triggers need a required string or integer payload field to deduplicate on")
        else:
            if senders_raw is not None and senders_raw != [self.owner]:
                self.error("E_TRIGGER_UNSCOPED", f"{path}.senders", f"{kind.value} triggers run only for the owner")
            senders = frozenset({self.owner})
            source = f"{kind.value}:{self.owner}"
            if kind is TriggerKind.SCHEDULE:
                schedule = raw.get("schedule")
                fields = schedule.split() if isinstance(schedule, str) else []
                if len(fields) != 5 or not all(_CRON_FIELD.fullmatch(f) for f in fields):
                    self.error("E_TRIGGER_SCHEDULE", f"{path}.schedule", "use a 5-field cron expression")
        return TriggerPolicy(kind=kind, source=source, senders=senders, payload_schema=payload_schema,
                             dedupe_key=dedupe, max_runs_per_hour=rate, schedule=schedule)

    def _steps(self, raw: Any) -> tuple[StepSpec, ...]:
        path = "$.steps"
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_STEPS:
            self.error("E_STEPS", path, f"a Recipe needs 1-{MAX_STEPS} steps")
            return ()
        for index, item in enumerate(raw):
            step_id = item.get("id") if isinstance(item, Mapping) else None
            if not isinstance(step_id, str) or not _STEP_ID.fullmatch(step_id):
                self.error("E_STEP_ID", f"{path}.{index}.id", "step ids are 1-32 lowercase letters, digits or _")
            elif step_id in self.declared_steps:
                self.error("E_DUPLICATE_STEP", f"{path}.{index}.id", f"step id {step_id!r} is used twice")
            else:
                self.declared_steps[step_id] = item

        parsed = [step for index, item in enumerate(raw)
                  if isinstance(item, Mapping) and (step := self._step(item, f"{path}.{index}")) is not None]
        return self._order(parsed)

    def _step(self, raw: Mapping[str, Any], path: str) -> StepSpec | None:
        self._reject_unknown(raw, _STEP_KEYS, path)
        step_id = raw.get("id")
        if not isinstance(step_id, str) or self.declared_steps.get(step_id) is not raw:
            return None
        cap = self._capability(raw.get("capability"), f"{path}.capability")
        if cap is None:
            return None
        if cap.shell_like:
            self.error("E_FORBIDDEN_CAPABILITY", f"{path}.capability",
                       f"{cap.id} runs shell or arbitrary commands, which managed Recipes never do")
        effect = cap.effective_effect
        if effect is Effect.UNKNOWN:
            self.error("E_UNCHARACTERIZED_EFFECT", f"{path}.capability",
                       f"{cap.id} has no trusted effect classification; it needs a Capalis contract test first")
        if "effect" in raw and raw["effect"] != effect.value:
            self.error("E_EFFECT_MISMATCH", f"{path}.effect",
                       f"the draft says {raw['effect']!r} but {cap.id} is classified {effect.value!r}")
        try:
            kind = StepKind(raw.get("kind", "action"))
        except ValueError:
            self.error("E_STEP_KIND", f"{path}.kind", "kind must be action or wait")
            return None

        deps: set[str] = set()
        inputs, target = self._inputs(cap, raw.get("inputs"), f"{path}.inputs", step_id, deps,
                                      allow_self=False)
        when = self._condition(raw.get("when"), f"{path}.when", step_id, deps, allow_self=False)
        for index, other in enumerate(raw.get("after") or []):
            if other not in self.declared_steps or other == step_id:
                self.error("E_BAD_REF", f"{path}.after.{index}", f"{other!r} is not another step")
            else:
                deps.add(other)

        idempotency = self._idempotency(cap, effect, raw.get("idempotency"), f"{path}.idempotency")
        retry = self._retry(raw.get("retry"), effect, idempotency, f"{path}.retry")
        timeout = _bounded_int(raw.get("timeout_s"), 60, 1, 600)
        if timeout is None:
            self.error("E_TIMEOUT", f"{path}.timeout_s", "timeout must be 1-600 seconds")
            timeout = 60

        wait = None
        if kind is StepKind.WAIT:
            if effect is not Effect.READ:
                self.error("E_WAIT_NOT_READ", f"{path}.kind", "wait steps may only poll read-only capabilities")
            wait = self._wait(raw.get("wait"), cap, f"{path}.wait", step_id, deps)
        elif "wait" in raw:
            self.error("E_UNKNOWN_FIELD", f"{path}.wait", "only wait steps take a wait policy")

        verify, reason = self._verification(raw, cap, effect, target, step_id, deps, path)
        return StepSpec(
            step_id=step_id, kind=kind, capability_id=cap.id, effect=effect, target=target, inputs=inputs,
            depends_on=tuple(sorted(deps)), when=when, idempotency=idempotency, retry=retry,
            timeout_s=timeout, verify=verify, unverifiable_reason=reason, wait=wait,
        )

    def _inputs(self, cap: CapabilityDescriptor, raw: Any, path: str, step_id: str, deps: set[str], *,
                allow_self: bool) -> tuple[dict[str, ValueRef], Target | None]:
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            self.error("E_INPUTS", path, "inputs must be an object")
            return {}, None
        properties: Mapping[str, Any] = cap.input_schema.get("properties") or {}
        closed = cap.input_schema.get("additionalProperties", True) is False or bool(properties)
        inputs: dict[str, ValueRef] = {}
        for name, raw_value in raw.items():
            param_path = f"{path}.{name}"
            if closed and name not in properties:
                self.error("E_UNKNOWN_INPUT", param_path, f"{cap.id} has no input named {name!r}")
                continue
            try:
                value = parse_value(raw_value)
            except ExpressionError as exc:
                self.error("E_BAD_VALUE", param_path, str(exc))
                continue
            self._check_value(value, properties.get(name), param_path, step_id, deps, allow_self=allow_self)
            inputs[name] = value
        for name in cap.input_schema.get("required") or ():
            if name not in inputs and name not in raw:
                self.error("E_MISSING_INPUT", f"{path}.{name}", f"{cap.id} requires {name!r}")

        target = None
        if cap.target is not None:
            selector = inputs.get(cap.target.param)
            if not isinstance(selector, Const):
                # D-013: where an effect lands is fixed at compile time; runtime data may never choose it.
                self.error("E_RUNTIME_TARGET", f"{path}.{cap.target.param}",
                           f"{cap.target.param!r} selects the {cap.target.kind} target and must be a constant")
            else:
                problem = target_problem(cap.target.kind, selector.value)
                if problem:
                    self.error("E_BAD_TARGET", f"{path}.{cap.target.param}", problem)
                else:
                    target = Target(cap.target.kind, selector.value)
        return inputs, target

    def _check_value(self, value: ValueRef, param_schema: Mapping[str, Any] | None, path: str, step_id: str,
                     deps: set[str], *, allow_self: bool) -> None:
        if isinstance(value, Const):
            if param_schema is not None:
                for message in jsonschema.validate(value.value, param_schema, path):
                    self.error("E_SCHEMA_MISMATCH", path, message)
            return
        if isinstance(value, Template):
            for ref in refs_in_value(value):
                self._ref_schema(ref, path, step_id, deps, allow_self=allow_self)
            if param_schema is not None and not jsonschema.assignable({"type": "string"}, param_schema):
                self.error("E_SCHEMA_MISMATCH", path, "a template produces text, but this input is not text")
            return
        options = value.options if isinstance(value, FirstOf) else (value,)
        for ref in options:
            source = self._ref_schema(ref, path, step_id, deps, allow_self=allow_self)
            if source is not None and not jsonschema.assignable(source, param_schema):
                self.error("E_SCHEMA_MISMATCH", path, f"{ref.text()} has an incompatible type")

    def _ref_schema(self, ref: Ref, path: str, step_id: str, deps: set[str], *,
                    allow_self: bool) -> Mapping[str, Any] | None:
        if ref.root == "trigger":
            found = jsonschema.subschema_at(self.trigger_schema, ref.path)
            if found is None:
                self.error("E_BAD_REF", path, f"{ref.text()} is not a declared trigger payload field")
            return found
        assert ref.step_id is not None
        if ref.step_id == step_id and not allow_self:
            self.error("E_SELF_REF", path, f"step {step_id!r} cannot read its own output before running")
            return None
        source_raw = self.declared_steps.get(ref.step_id)
        source_id = source_raw.get("capability") if source_raw else None
        source_cap = self.capabilities.get(source_id) if isinstance(source_id, str) else None
        if source_raw is None or source_cap is None:
            self.error("E_BAD_REF", path, f"{ref.text()} refers to an unknown step")
            return None
        found = jsonschema.subschema_at(source_cap.output_schema, ref.path) if ref.path else source_cap.output_schema
        if found is None:
            self.error("E_BAD_REF", path, f"{ref.text()} is not a declared output of {source_cap.id}")
        if ref.step_id != step_id:
            deps.add(ref.step_id)
        return found

    def _condition(self, raw: Any, path: str, step_id: str, deps: set[str], *,
                   allow_self: bool) -> Condition | None:
        if raw is None:
            return None
        try:
            condition = parse_condition(raw)
        except ExpressionError as exc:
            self.error("E_BAD_CONDITION", path, str(exc))
            return None
        for ref in refs_in_condition(condition):
            self._ref_schema(ref, path, step_id, deps, allow_self=allow_self)
        return condition

    def _idempotency(self, cap: CapabilityDescriptor, effect: Effect, raw: Any, path: str) -> IdempotencyStrategy:
        if effect is Effect.READ:
            if raw not in (None, IdempotencyStrategy.NOT_NEEDED.value):
                self.error("E_IDEMPOTENCY", path, "read-only steps need no idempotency strategy")
            return IdempotencyStrategy.NOT_NEEDED
        default = {IdempotencySupport.NATIVE_KEY: IdempotencyStrategy.NATIVE_KEY,
                   IdempotencySupport.RECONCILE: IdempotencyStrategy.RECONCILE}.get(cap.idempotency,
                                                                                    IdempotencyStrategy.NONE)
        try:
            strategy = IdempotencyStrategy(raw) if raw is not None else default
        except ValueError:
            self.error("E_IDEMPOTENCY", path, "use native_key, reconcile or none")
            return default
        if strategy is IdempotencyStrategy.NOT_NEEDED:
            self.error("E_IDEMPOTENCY", path, "a step with side effects needs native_key, reconcile or none")
        elif strategy is IdempotencyStrategy.NATIVE_KEY and cap.idempotency is not IdempotencySupport.NATIVE_KEY:
            self.error("E_IDEMPOTENCY", path, f"{cap.id} does not support provider idempotency keys")
        elif strategy is IdempotencyStrategy.RECONCILE:
            reconcile = self.capabilities.get(cap.reconcile_with or "")
            if reconcile is None or reconcile.effective_effect is not Effect.READ:
                self.error("E_IDEMPOTENCY", path, f"{cap.id} has no trusted read-only reconciliation lookup")
            elif (reconcile.target is None or cap.target is None or reconcile.target.kind != cap.target.kind
                  or "idempotency_key" not in (reconcile.input_schema.get("properties") or {})):
                # The lookup must search the same target the write lands on, by the write's key;
                # otherwise "not found" would wrongly license a second write.
                self.error("E_IDEMPOTENCY", path,
                           f"{reconcile.id} must look up the same {cap.target.kind if cap.target else ''} "
                           "target by idempotency_key")
            elif not _reconcile_output_fits(reconcile.output_schema, cap.output_schema):
                # A confirmed effect must yield the same output a successful write would have, so the
                # rest of the Recipe can continue from it.
                self.error("E_IDEMPOTENCY", path,
                           f"{reconcile.id} must return found (boolean) and the {cap.id} output as resource")
            else:
                self.used[reconcile.id] = reconcile
        return strategy

    def _retry(self, raw: Any, effect: Effect, idempotency: IdempotencyStrategy, path: str) -> RetryPolicy:
        if raw is None:
            return RetryPolicy()
        if not isinstance(raw, Mapping) or set(raw) - {"max_attempts", "backoff_s"}:
            self.error("E_RETRY", path, "retry takes max_attempts and backoff_s")
            return RetryPolicy()
        attempts = _bounded_int(raw.get("max_attempts"), 1, 1, 5)
        backoff = _bounded_int(raw.get("backoff_s"), 0, 0, 3600)
        if attempts is None or backoff is None:
            self.error("E_RETRY", path, "max_attempts must be 1-5 and backoff_s 0-3600")
            return RetryPolicy()
        if attempts > 1 and effect is not Effect.READ and idempotency is IdempotencyStrategy.NONE:
            self.error("E_UNSAFE_RETRY", path,
                       "a side effect without idempotency keys or reconciliation cannot be retried safely")
        return RetryPolicy(attempts, backoff)

    def _wait(self, raw: Any, cap: CapabilityDescriptor, path: str, step_id: str,
              deps: set[str]) -> WaitPolicy | None:
        if not isinstance(raw, Mapping) or set(raw) - {"until", "poll_interval_s", "deadline_s"}:
            self.error("E_WAIT", path, "wait steps take until, poll_interval_s and deadline_s")
            return None
        until = self._condition(raw.get("until"), f"{path}.until", step_id, deps, allow_self=True)
        if until is None:
            self.error("E_WAIT", f"{path}.until", "a wait step needs a completion condition")
        poll = _bounded_int(raw.get("poll_interval_s"), 300, 30, 86400)
        deadline = _bounded_int(raw["deadline_s"], 0, 60, THIRTY_DAYS) if "deadline_s" in raw else None
        if poll is None or deadline is None:
            self.error("E_WAIT", path, "poll every 30s-1d and set a deadline between 60s and 30 days")
            return None
        if until is None:
            return None
        return WaitPolicy(until=until, poll_interval_s=poll, deadline_s=deadline)

    def _verification(self, raw: Mapping[str, Any], cap: CapabilityDescriptor, effect: Effect,
                      target: Target | None, step_id: str, deps: set[str],
                      path: str) -> tuple[tuple[OutcomeContract, ...], str | None]:
        contracts_raw = raw.get("verify") or []
        reason = raw.get("unverifiable_reason")
        if effect is Effect.READ:
            if contracts_raw or reason:
                self.error("E_VERIFY_ON_READ", f"{path}.verify", "read-only steps are not outcome-verified")
            return (), None
        if not isinstance(contracts_raw, list) or len(contracts_raw) > MAX_CONTRACTS_PER_STEP:
            self.error("E_VERIFY", f"{path}.verify", f"verify takes up to {MAX_CONTRACTS_PER_STEP} contracts")
            return (), None
        if reason is not None and (not isinstance(reason, str) or not 10 <= len(reason) <= 300):
            self.error("E_VERIFY", f"{path}.unverifiable_reason", "explain in 10-300 characters")
            reason = None
        if effect in EXTERNAL_EFFECTS and not contracts_raw and reason is None:
            self.error("E_MISSING_VERIFIER", f"{path}.verify",
                       "an external effect needs an outcome check, or an explicit unverifiable_reason")
        contracts = []
        seen: set[str] = set()
        for index, item in enumerate(contracts_raw):
            contract = self._contract(item, cap, target, step_id, deps, f"{path}.verify.{index}")
            if contract is None:
                continue
            if contract.contract_id in seen:
                self.error("E_VERIFY", f"{path}.verify.{index}.id", "contract ids must be unique per step")
            seen.add(contract.contract_id)
            contracts.append(contract)
        return tuple(contracts), reason

    def _contract(self, raw: Any, cap: CapabilityDescriptor, step_target: Target | None, step_id: str,
                  deps: set[str], path: str) -> OutcomeContract | None:
        if not isinstance(raw, Mapping):
            self.error("E_VERIFY", path, "a contract must be an object")
            return None
        self._reject_unknown(raw, _CONTRACT_KEYS, path)
        contract_id = raw.get("id")
        if not isinstance(contract_id, str) or not _STEP_ID.fullmatch(contract_id):
            self.error("E_VERIFY", f"{path}.id", "contract ids are 1-32 lowercase letters, digits or _")
            return None
        probe = self._capability(raw.get("probe"), f"{path}.probe")
        if probe is None:
            return None
        if probe.effective_effect is not Effect.READ:
            self.error("E_PROBE_NOT_READONLY", f"{path}.probe", f"{probe.id} is not a trusted read-only capability")
        if probe.id == cap.id:
            # I-03: the write's own response is never its proof; observation must be a separate call.
            self.error("E_PROBE_NOT_INDEPENDENT", f"{path}.probe", "the verifier must be a different capability")
        inputs, probe_target = self._inputs(probe, raw.get("inputs"), f"{path}.inputs", step_id, deps,
                                            allow_self=True)
        try:
            level = EvidenceLevel(raw.get("level", "V1"))
        except ValueError:
            level = EvidenceLevel.V0
        if level is EvidenceLevel.V0:
            self.error("E_BAD_LEVEL", f"{path}.level", "a contract requires V1 read-back or V2 cross-system evidence")
        elif level is EvidenceLevel.V1 and step_target is not None and probe_target != step_target:
            self.error("E_VERIFIER_TARGET_MISMATCH", f"{path}.inputs",
                       f"V1 read-back must observe the same target ({step_target}) the step changed")
        elif level is EvidenceLevel.V2 and (probe_target is None or step_target is None
                                            or probe_target.kind == step_target.kind):
            self.error("E_V2_NOT_CROSS_SYSTEM", f"{path}.level",
                       "V2 evidence must come from a different system than the one the step changed")

        expect_raw = raw.get("expect")
        expectations: list[Expectation] = []
        if not isinstance(expect_raw, list) or not 1 <= len(expect_raw) <= MAX_EXPECTATIONS:
            self.error("E_VERIFY", f"{path}.expect", f"list 1-{MAX_EXPECTATIONS} expectations")
            expect_raw = []
        for index, item in enumerate(expect_raw):
            expectation = self._expectation(item, probe, step_id, deps, f"{path}.expect.{index}")
            if expectation is not None:
                expectations.append(expectation)

        deadline = _bounded_int(raw.get("deadline_s"), 300, 1, THIRTY_DAYS)
        freshness = _bounded_int(raw.get("freshness_s"), 600, 1, THIRTY_DAYS)
        required = raw.get("required", True)
        if deadline is None or freshness is None or not isinstance(required, bool):
            self.error("E_VERIFY", path, "deadline_s and freshness_s are 1s-30d; required is true or false")
            return None
        return OutcomeContract(contract_id=contract_id, probe=probe.id, inputs=inputs,
                               expect=tuple(expectations), level=level, probe_target=probe_target,
                               deadline_s=deadline, freshness_s=freshness, required=required)

    def _expectation(self, raw: Any, probe: CapabilityDescriptor, step_id: str, deps: set[str],
                     path: str) -> Expectation | None:
        if not isinstance(raw, Mapping) or set(raw) - {"path", "op", "value"}:
            self.error("E_VERIFY", path, "an expectation takes path, op and value")
            return None
        field_path = _parse_path(raw.get("path"))
        field_schema = jsonschema.subschema_at(probe.output_schema, field_path) if field_path else None
        if field_path is None or field_schema is None:
            self.error("E_BAD_REF", f"{path}.path", f"not a declared output field of {probe.id}")
            return None
        op = raw.get("op")
        if op not in _EXPECT_OPS:
            self.error("E_VERIFY", f"{path}.op", f"op must be one of {sorted(_EXPECT_OPS)}")
            return None
        if op == "exists":
            if "value" in raw:
                self.error("E_VERIFY", f"{path}.value", "'exists' takes no value")
            return Expectation(field_path, op)
        if "value" not in raw:
            self.error("E_VERIFY", f"{path}.value", f"'{op}' needs a value")
            return None
        try:
            value = parse_value(raw["value"])
        except ExpressionError as exc:
            self.error("E_BAD_VALUE", f"{path}.value", str(exc))
            return None
        self._check_value(value, None, f"{path}.value", step_id, deps, allow_self=True)
        return Expectation(field_path, op, value)

    def _budgets(self, raw: Any, steps: tuple[StepSpec, ...]) -> Budgets | None:
        raw = raw or {}
        if not isinstance(raw, Mapping) or set(raw) - {"max_writes_per_run", "max_runs_per_day",
                                                       "spend_limit_cents"}:
            self.error("E_BUDGETS", "$.budgets", "budgets take max_writes_per_run, max_runs_per_day, "
                                                 "spend_limit_cents")
            return None
        writes = _bounded_int(raw.get("max_writes_per_run"), 10, 1, 50)
        runs = _bounded_int(raw.get("max_runs_per_day"), 50, 1, 1000)
        spend = _bounded_int(raw.get("spend_limit_cents"), 0, 0, 1_000_000)
        if writes is None or runs is None or spend is None:
            self.error("E_BUDGETS", "$.budgets", "writes 1-50 per run, runs 1-1000 per day, spend >= 0 cents")
            return None
        side_effects = sum(1 for step in steps if step.is_side_effect)
        if side_effects > writes:
            self.error("E_BUDGET_TOO_SMALL", "$.budgets.max_writes_per_run",
                       f"the Recipe has {side_effects} side-effect steps but allows only {writes} writes")
        if spend == 0 and any(step.effect is Effect.SPEND for step in steps):
            self.error("E_SPEND_WITHOUT_LIMIT", "$.budgets.spend_limit_cents", "a spending step needs a limit")
        return Budgets(writes, runs, spend)

    def _approval(self, raw: Any, steps: tuple[StepSpec, ...]) -> ApprovalPolicy | None:
        raw = raw or {}
        if not isinstance(raw, Mapping) or set(raw) - {"per_run_confirmation", "grant_ttl_s"}:
            self.error("E_APPROVAL", "$.approval", "approval takes per_run_confirmation and grant_ttl_s")
            return None
        try:
            confirm = frozenset(Effect(value) for value in
                                raw.get("per_run_confirmation", [Effect.SPEND.value, Effect.DELETE_OR_REVOKE.value]))
        except (ValueError, TypeError):
            self.error("E_APPROVAL", "$.approval.per_run_confirmation", "list effect classes")
            return None
        missing = {step.effect for step in steps} & {Effect.SPEND, Effect.DELETE_OR_REVOKE} - confirm
        if missing:
            self.error("E_CONFIRMATION_REQUIRED", "$.approval.per_run_confirmation",
                       f"{sorted(e.value for e in missing)} effects need confirmation on every run")
        ttl = _bounded_int(raw.get("grant_ttl_s"), THIRTY_DAYS, 3600, NINETY_DAYS)
        if ttl is None:
            self.error("E_APPROVAL", "$.approval.grant_ttl_s", "a standing grant lasts 1 hour to 90 days")
            return None
        return ApprovalPolicy(confirm, ttl)

    def _order(self, steps: list[StepSpec]) -> tuple[StepSpec, ...]:
        by_id = {step.step_id: step for step in steps}
        position = {step_id: index for index, step_id in enumerate(self.declared_steps)}
        remaining = {step.step_id: {d for d in step.depends_on if d in by_id} for step in steps}
        ordered: list[StepSpec] = []
        while remaining:
            ready = sorted((sid for sid, deps in remaining.items() if not deps), key=position.__getitem__)
            if not ready:
                cycle = ", ".join(sorted(remaining))
                self.error("E_CYCLE", "$.steps", f"steps depend on each other in a cycle: {cycle}")
                return tuple(ordered)
            for sid in ready:
                ordered.append(by_id[sid])
                del remaining[sid]
            for deps in remaining.values():
                deps.difference_update(ready)
        return tuple(ordered)
