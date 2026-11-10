"""Fail-closed authorization for Capalis-managed runs and steps.

These checks are the enforcement boundary for managed execution (D-005, D-016): Hermes hooks and
middleware do not run on ``ctx.dispatch_tool``, so a step that is not explicitly allowed here must
never reach a dispatch adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .drift import DriftReport
from .lifecycle import RecipeState
from .model import ApprovalGrant, Effect, RecipeSpec, StepSpec

SHADOW_RUNNABLE = frozenset({
    RecipeState.COMPILED, RecipeState.TESTED, RecipeState.SHADOW_QUALIFIED,
    RecipeState.AWAITING_APPROVAL, RecipeState.LIVE, RecipeState.PAUSED,
})


class Mode(StrEnum):
    LIVE = "live"
    SHADOW = "shadow"


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    code: str
    detail: str
    simulate: bool = False


def _deny(code: str, detail: str) -> Decision:
    return Decision(False, code, detail)


@dataclass(frozen=True, slots=True)
class RunContext:
    now: int
    mode: Mode
    recipe_state: RecipeState
    global_paused: bool
    grant: ApprovalGrant | None
    drift: DriftReport | None
    runs_last_hour: int
    runs_today: int


@dataclass(frozen=True, slots=True)
class TriggerEvent:
    source: str
    sender: str
    dedupe_value: str | None


@dataclass(frozen=True, slots=True)
class StepContext:
    now: int
    mode: Mode
    recipe_state: RecipeState
    global_paused: bool
    grant: ApprovalGrant | None
    drift: DriftReport | None
    trigger_source: str
    writes_dispatched: int
    confirmed_steps: frozenset[str] = frozenset()


def grant_problem(grant: ApprovalGrant | None, recipe: RecipeSpec, now: int) -> Decision | None:
    if grant is None:
        return _deny("no_grant", "this Recipe version has no approval")
    if grant.recipe_id != recipe.recipe_id or grant.recipe_digest != recipe.digest:
        return _deny("grant_wrong_version", "the approval was given for a different Recipe version")
    if grant.revoked_at is not None:
        return _deny("grant_revoked", "the approval was revoked")
    if not grant.issued_at <= now < grant.expires_at:
        return _deny("grant_expired", "the approval is not valid at this time")
    return None


def authorize_run(recipe: RecipeSpec, event: TriggerEvent, ctx: RunContext) -> Decision:
    """Ingress guard (I-09): origin, sender, rate and recipe state, before any run is scheduled."""
    if event.source != recipe.trigger.source:
        return _deny("wrong_source", "the event did not come from this Recipe's trigger source")
    if event.sender not in recipe.trigger.senders:
        return _deny("unauthorized_sender", "the sender is not authorized to trigger this Recipe")
    if recipe.trigger.dedupe_key is not None and not event.dedupe_value:
        return _deny("no_dedupe_value", "the event has no value to deduplicate on")
    # Drift blocks shadow runs too: qualifying against changed dependencies would prove nothing.
    if ctx.drift is not None and ctx.drift.blocking:
        return _deny("drift", ctx.drift.summary())
    if ctx.mode is Mode.SHADOW:
        if ctx.recipe_state not in SHADOW_RUNNABLE:
            return _deny("state", f"a {ctx.recipe_state.value} Recipe cannot run, even in shadow mode")
        return Decision(True, "ok", "shadow run: writes are simulated", simulate=True)
    if ctx.global_paused:
        return _deny("global_pause", "all Capalis Recipes are paused")
    if ctx.recipe_state is not RecipeState.LIVE:
        return _deny("state", f"the Recipe is {ctx.recipe_state.value}, not live")
    problem = grant_problem(ctx.grant, recipe, ctx.now)
    if problem is not None:
        return problem
    assert ctx.grant is not None
    if recipe.trigger.source not in ctx.grant.trigger_sources:
        return _deny("grant_scope_source", "the approval does not cover this trigger source")
    if ctx.runs_last_hour >= recipe.trigger.max_runs_per_hour:
        return _deny("rate_hour", "the hourly run limit is reached")
    if ctx.runs_today >= recipe.budgets.max_runs_per_day:
        return _deny("rate_day", "the daily run limit is reached")
    return Decision(True, "ok", "authorized")


def authorize_step(recipe: RecipeSpec, step: StepSpec, ctx: StepContext) -> Decision:
    """Pre-dispatch guard (I-02, I-05, I-07, I-11), evaluated again before every managed call."""
    if recipe.step(step.step_id) != step:
        return _deny("step_not_in_recipe", "the step does not match the compiled Recipe")
    if step.effect is Effect.UNKNOWN:
        return _deny("unknown_effect", "steps with uncharacterized effects never run")
    if ctx.drift is not None and ctx.drift.blocking:
        return _deny("drift", ctx.drift.summary())
    if ctx.mode is Mode.SHADOW:
        if ctx.recipe_state not in SHADOW_RUNNABLE:
            return _deny("state", f"a {ctx.recipe_state.value} Recipe cannot run, even in shadow mode")
        return Decision(True, "ok", "shadow", simulate=step.is_side_effect)
    if ctx.global_paused:
        return _deny("global_pause", "all Capalis Recipes are paused")
    if ctx.recipe_state is not RecipeState.LIVE:
        return _deny("state", f"the Recipe is {ctx.recipe_state.value}, not live")
    problem = grant_problem(ctx.grant, recipe, ctx.now)
    if problem is not None:
        return problem
    assert ctx.grant is not None
    if step.effect not in ctx.grant.effects:
        return _deny("grant_scope_effect", f"the approval does not cover {step.effect.value} effects")
    if step.target is not None and step.target not in ctx.grant.targets:
        return _deny("grant_scope_target", f"the approval does not cover {step.target}")
    if ctx.trigger_source not in ctx.grant.trigger_sources:
        return _deny("grant_scope_source", "the approval does not cover this trigger source")
    if step.is_side_effect and ctx.writes_dispatched >= recipe.budgets.max_writes_per_run:
        return _deny("write_budget", "the per-run write budget is used up")
    if step.effect in recipe.approval.per_run_confirmation and step.step_id not in ctx.confirmed_steps:
        return _deny("needs_confirmation", f"{step.effect.value} steps need confirmation on every run")
    return Decision(True, "ok", "authorized")


def authorize_probe(recipe: RecipeSpec, step: StepSpec, contract_id: str, ctx: StepContext) -> Decision:
    """Verification probes are reads, but still only within the approved targets."""
    contract = next((c for c in step.verify if c.contract_id == contract_id), None)
    if recipe.step(step.step_id) != step or contract is None:
        return _deny("probe_not_in_recipe", "the probe does not match the compiled Recipe")
    if ctx.mode is Mode.SHADOW:
        return Decision(True, "ok", "shadow")
    problem = grant_problem(ctx.grant, recipe, ctx.now)
    if problem is not None:
        return problem
    assert ctx.grant is not None
    if contract.probe_target is not None and contract.probe_target not in ctx.grant.targets:
        return _deny("grant_scope_target", f"the approval does not cover {contract.probe_target}")
    return Decision(True, "ok", "authorized")
