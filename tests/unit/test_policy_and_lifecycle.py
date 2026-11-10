from __future__ import annotations

from dataclasses import replace

import pytest

from capalis.core.drift import DriftChange, DriftKind, DriftReport
from capalis.core.lifecycle import (
    RECIPE_TRANSITIONS,
    IllegalTransition,
    RecipeState,
    RunState,
    StepState,
    recipe_transition,
    run_transition,
    step_transition,
)
from capalis.core.model import Effect, RecipeSpec, Target, grant_for
from capalis.core.policy import (
    Mode,
    RunContext,
    StepContext,
    TriggerEvent,
    authorize_probe,
    authorize_run,
    authorize_step,
)
from support import DISCORD, REPORTER, SLACK, compile_draft, hero_draft

NOW = 1_800_000_000
SOURCE = f"discord:channel:{DISCORD}"


@pytest.fixture
def recipe() -> RecipeSpec:
    compiled = compile_draft(hero_draft()).recipe
    assert compiled is not None
    return compiled


def step_ctx(recipe: RecipeSpec, **overrides: object) -> StepContext:
    values: dict[str, object] = dict(
        now=NOW, mode=Mode.LIVE, recipe_state=RecipeState.LIVE, global_paused=False,
        grant=grant_for(recipe, grant_id="g1", actor="owner", now=NOW - 10), drift=None,
        trigger_source=SOURCE, writes_dispatched=0,
    )
    values.update(overrides)
    return StepContext(**values)  # type: ignore[arg-type]


def run_ctx(recipe: RecipeSpec, **overrides: object) -> RunContext:
    values: dict[str, object] = dict(
        now=NOW, mode=Mode.LIVE, recipe_state=RecipeState.LIVE, global_paused=False,
        grant=grant_for(recipe, grant_id="g1", actor="owner", now=NOW - 10), drift=None,
        runs_last_hour=0, runs_today=0,
    )
    values.update(overrides)
    return RunContext(**values)  # type: ignore[arg-type]


EVENT = TriggerEvent(source=SOURCE, sender=REPORTER, dedupe_value="m-1")


def test_authorized_run_and_steps(recipe: RecipeSpec) -> None:
    assert authorize_run(recipe, EVENT, run_ctx(recipe)).allowed
    for step in recipe.steps:
        decision = authorize_step(recipe, step, step_ctx(recipe))
        assert decision.allowed and not decision.simulate, (step.step_id, decision)


@pytest.mark.parametrize(("event", "overrides", "code"), [
    (replace(EVENT, source="discord:channel:999999999999999999"), {}, "wrong_source"),
    (replace(EVENT, sender="111111111111111111"), {}, "unauthorized_sender"),
    (replace(EVENT, dedupe_value=None), {}, "no_dedupe_value"),
    (EVENT, {"global_paused": True}, "global_pause"),
    (EVENT, {"recipe_state": RecipeState.PAUSED}, "state"),
    (EVENT, {"recipe_state": RecipeState.QUARANTINED}, "state"),
    (EVENT, {"grant": None}, "no_grant"),
    (EVENT, {"runs_last_hour": 20}, "rate_hour"),
    (EVENT, {"runs_today": 100}, "rate_day"),
])
def test_run_denials(recipe: RecipeSpec, event: TriggerEvent, overrides: dict[str, object], code: str) -> None:
    decision = authorize_run(recipe, event, run_ctx(recipe, **overrides))
    assert not decision.allowed
    assert decision.code == code


def test_run_denied_on_drift(recipe: RecipeSpec) -> None:
    drift = DriftReport(recipe.digest, (DriftChange("capalis:slack.message.post", DriftKind.SCHEMA_CHANGED, "a", "b"),))
    assert authorize_run(recipe, EVENT, run_ctx(recipe, drift=drift)).code == "drift"
    create = recipe.step("create")
    assert create is not None
    assert authorize_step(recipe, create, step_ctx(recipe, drift=drift)).code == "drift"


def test_grant_is_version_bound_expiring_and_revocable(recipe: RecipeSpec) -> None:
    create = recipe.step("create")
    assert create is not None
    grant = grant_for(recipe, grant_id="g1", actor="owner", now=NOW - 10)
    other_version = compile_draft(hero_draft(), version=2).recipe
    assert other_version is not None
    cases = {
        "grant_wrong_version": grant_for(other_version, grant_id="g2", actor="owner", now=NOW - 10),
        "grant_revoked": replace(grant, revoked_at=NOW - 1),
        "grant_expired": replace(grant, expires_at=NOW),
    }
    for code, bad in cases.items():
        decision = authorize_step(recipe, create, step_ctx(recipe, grant=bad))
        assert (decision.allowed, decision.code) == (False, code)


def test_forged_step_or_target_never_authorized(recipe: RecipeSpec) -> None:
    create = recipe.step("create")
    assert create is not None
    forged = replace(create, target=Target("github.repo", "attacker/repo"))
    assert authorize_step(recipe, forged, step_ctx(recipe)).code == "step_not_in_recipe"
    renamed = replace(create, capability_id="capalis:github.issue.delete")
    assert authorize_step(recipe, renamed, step_ctx(recipe)).code == "step_not_in_recipe"


def test_grant_scope_effect_and_target(recipe: RecipeSpec) -> None:
    notify = recipe.step("notify")
    assert notify is not None
    grant = grant_for(recipe, grant_id="g1", actor="owner", now=NOW - 10)
    no_send = replace(grant, effects=grant.effects - {Effect.EXTERNAL_SEND})
    assert authorize_step(recipe, notify, step_ctx(recipe, grant=no_send)).code == "grant_scope_effect"
    no_channel = replace(grant, targets=grant.targets - {Target("slack.channel", SLACK)})
    assert authorize_step(recipe, notify, step_ctx(recipe, grant=no_channel)).code == "grant_scope_target"
    assert authorize_probe(recipe, notify, "message_readback",
                           step_ctx(recipe, grant=no_channel)).code == "grant_scope_target"
    other_source = replace(grant, trigger_sources=frozenset({"discord:channel:1"}))
    assert authorize_step(recipe, notify, step_ctx(recipe, grant=other_source)).code == "grant_scope_source"


def test_write_budget_and_pause(recipe: RecipeSpec) -> None:
    notify = recipe.step("notify")
    assert notify is not None
    assert authorize_step(recipe, notify, step_ctx(recipe, writes_dispatched=4)).code == "write_budget"
    assert authorize_step(recipe, notify, step_ctx(recipe, recipe_state=RecipeState.PAUSED)).code == "state"
    assert authorize_step(recipe, notify, step_ctx(recipe, global_paused=True)).code == "global_pause"


def test_shadow_mode_simulates_every_side_effect(recipe: RecipeSpec) -> None:
    ctx = step_ctx(recipe, mode=Mode.SHADOW, recipe_state=RecipeState.TESTED, grant=None)
    for step in recipe.steps:
        decision = authorize_step(recipe, step, ctx)
        assert decision.allowed
        assert decision.simulate is step.is_side_effect
    retired = step_ctx(recipe, mode=Mode.SHADOW, recipe_state=RecipeState.QUARANTINED)
    assert not authorize_step(recipe, recipe.steps[0], retired).allowed


def test_recipe_lifecycle_edges() -> None:
    path = [RecipeState.COMPILED, RecipeState.TESTED, RecipeState.SHADOW_QUALIFIED,
            RecipeState.AWAITING_APPROVAL, RecipeState.LIVE, RecipeState.PAUSED, RecipeState.LIVE,
            RecipeState.QUARANTINED, RecipeState.RETIRED]
    state = RecipeState.DRAFT
    for nxt in path:
        state = recipe_transition(state, nxt)
    with pytest.raises(IllegalTransition):
        recipe_transition(RecipeState.QUARANTINED, RecipeState.LIVE)
    with pytest.raises(IllegalTransition):
        recipe_transition(RecipeState.COMPILED, RecipeState.LIVE)


def test_quarantined_recipe_can_never_reach_live_again() -> None:
    reachable = {RecipeState.QUARANTINED}
    frontier = [RecipeState.QUARANTINED]
    while frontier:
        for nxt in RECIPE_TRANSITIONS[frontier.pop()]:
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    assert reachable == {RecipeState.QUARANTINED, RecipeState.RETIRED}


def test_live_is_only_reachable_through_approval() -> None:
    sources = {state for state, nxt in RECIPE_TRANSITIONS.items() if RecipeState.LIVE in nxt}
    assert sources == {RecipeState.AWAITING_APPROVAL, RecipeState.PAUSED}


def test_run_and_step_terminal_states_are_final() -> None:
    with pytest.raises(IllegalTransition):
        run_transition(RunState.VERIFIED, RunState.RUNNING)
    with pytest.raises(IllegalTransition):
        step_transition(StepState.COMPLETED, StepState.DISPATCHED)
    # An unknown effect is never re-sent as the same attempt; it is reconciled, and any retry is a new attempt.
    with pytest.raises(IllegalTransition):
        step_transition(StepState.UNKNOWN_EFFECT, StepState.INTENT_RECORDED)
    with pytest.raises(IllegalTransition):
        step_transition(StepState.UNKNOWN_EFFECT, StepState.DISPATCHED)
    assert step_transition(StepState.UNKNOWN_EFFECT, StepState.WAITING) is StepState.WAITING
    with pytest.raises(IllegalTransition):
        step_transition(StepState.PLANNED, StepState.OBSERVED_VERIFIED)
