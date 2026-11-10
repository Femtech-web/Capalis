"""Recipe, run and step state machines as explicit transition tables.

A quarantined Recipe never returns to live: repair produces a new version that is compiled, tested
and approved again (FR-DRI-05). Guards such as "the grant is still valid" belong to policy; this module
only decides which edges exist.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import TypeVar


class RecipeState(StrEnum):
    DRAFT = "draft"
    COMPILED = "compiled"
    TESTED = "tested"
    SHADOW_QUALIFIED = "shadow_qualified"
    AWAITING_APPROVAL = "awaiting_approval"
    LIVE = "live"
    PAUSED = "paused"
    QUARANTINED = "quarantined"
    RETIRED = "retired"


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_RECONCILIATION = "needs_reconciliation"


class StepState(StrEnum):
    PLANNED = "planned"
    SKIPPED = "skipped"
    SIMULATED = "simulated"
    BLOCKED = "blocked"
    INTENT_RECORDED = "intent_recorded"
    DISPATCHED = "dispatched"
    WAITING = "waiting"
    COMPLETED = "completed"
    OBSERVED_VERIFIED = "observed_verified"
    UNVERIFIED = "unverified"
    FAILED = "failed"
    UNKNOWN_EFFECT = "unknown_effect"


_R = RecipeState
RECIPE_TRANSITIONS: Mapping[RecipeState, frozenset[RecipeState]] = {
    _R.DRAFT: frozenset({_R.COMPILED, _R.RETIRED}),
    _R.COMPILED: frozenset({_R.TESTED, _R.RETIRED}),
    _R.TESTED: frozenset({_R.SHADOW_QUALIFIED, _R.QUARANTINED, _R.RETIRED}),
    _R.SHADOW_QUALIFIED: frozenset({_R.AWAITING_APPROVAL, _R.QUARANTINED, _R.RETIRED}),
    _R.AWAITING_APPROVAL: frozenset({_R.LIVE, _R.QUARANTINED, _R.RETIRED}),
    _R.LIVE: frozenset({_R.PAUSED, _R.QUARANTINED, _R.AWAITING_APPROVAL, _R.RETIRED}),
    _R.PAUSED: frozenset({_R.LIVE, _R.QUARANTINED, _R.AWAITING_APPROVAL, _R.RETIRED}),
    _R.QUARANTINED: frozenset({_R.RETIRED}),
    _R.RETIRED: frozenset(),
}

_U = RunState
RUN_TRANSITIONS: Mapping[RunState, frozenset[RunState]] = {
    _U.QUEUED: frozenset({_U.RUNNING, _U.CANCELLED}),
    _U.RUNNING: frozenset({_U.WAITING, _U.VERIFIED, _U.UNVERIFIED, _U.FAILED, _U.CANCELLED,
                           _U.NEEDS_RECONCILIATION}),
    _U.WAITING: frozenset({_U.RUNNING, _U.UNVERIFIED, _U.FAILED, _U.CANCELLED}),
    _U.NEEDS_RECONCILIATION: frozenset({_U.RUNNING, _U.UNVERIFIED, _U.FAILED, _U.CANCELLED}),
    _U.VERIFIED: frozenset(),
    _U.UNVERIFIED: frozenset(),
    _U.FAILED: frozenset(),
    _U.CANCELLED: frozenset(),
}

_S = StepState
STEP_TRANSITIONS: Mapping[StepState, frozenset[StepState]] = {
    _S.PLANNED: frozenset({_S.SKIPPED, _S.SIMULATED, _S.BLOCKED, _S.INTENT_RECORDED, _S.DISPATCHED}),
    _S.INTENT_RECORDED: frozenset({_S.DISPATCHED, _S.BLOCKED, _S.FAILED}),
    _S.DISPATCHED: frozenset({_S.WAITING, _S.COMPLETED, _S.OBSERVED_VERIFIED, _S.UNVERIFIED, _S.FAILED,
                              _S.UNKNOWN_EFFECT}),
    _S.WAITING: frozenset({_S.DISPATCHED, _S.COMPLETED, _S.OBSERVED_VERIFIED, _S.UNVERIFIED, _S.FAILED}),
    # Reconciliation resolves an unknown effect: confirmed (then verified like any write), or proven
    # never applied (failed; only a new attempt may retry it). Nothing else leaves this state.
    _S.UNKNOWN_EFFECT: frozenset({_S.WAITING, _S.OBSERVED_VERIFIED, _S.COMPLETED, _S.UNVERIFIED, _S.FAILED}),
    _S.SKIPPED: frozenset(),
    _S.SIMULATED: frozenset(),
    _S.BLOCKED: frozenset(),
    _S.COMPLETED: frozenset(),
    _S.OBSERVED_VERIFIED: frozenset(),
    _S.UNVERIFIED: frozenset(),
    _S.FAILED: frozenset(),
}

RUN_TERMINAL = frozenset(state for state, nxt in RUN_TRANSITIONS.items() if not nxt)
STEP_TERMINAL = frozenset(state for state, nxt in STEP_TRANSITIONS.items() if not nxt)

S = TypeVar("S", RecipeState, RunState, StepState)


class IllegalTransition(ValueError):
    pass


def _check(table: Mapping[S, frozenset[S]], current: S, target: S) -> S:
    if target not in table[current]:
        raise IllegalTransition(f"{type(current).__name__} {current.value} -> {target.value} is not allowed")
    return target


def recipe_transition(current: RecipeState, target: RecipeState) -> RecipeState:
    return _check(RECIPE_TRANSITIONS, current, target)


def run_transition(current: RunState, target: RunState) -> RunState:
    return _check(RUN_TRANSITIONS, current, target)


def step_transition(current: StepState, target: StepState) -> StepState:
    return _check(STEP_TRANSITIONS, current, target)
