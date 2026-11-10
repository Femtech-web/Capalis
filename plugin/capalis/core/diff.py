"""What changed between two versions of a Recipe, in words an approver can check (FR-REC-06)."""

from __future__ import annotations

from typing import Any

from .model import RecipeSpec


def _step_wire(recipe: RecipeSpec) -> dict[str, dict[str, Any]]:
    return {step["id"]: step for step in recipe.to_wire()["steps"]}


def diff_recipes(old: RecipeSpec, new: RecipeSpec) -> list[str]:
    changes: list[str] = []
    if old.trigger.source != new.trigger.source:
        changes.append(f"trigger moved from {old.trigger.source} to {new.trigger.source}")
    added_senders = new.trigger.senders - old.trigger.senders
    removed_senders = old.trigger.senders - new.trigger.senders
    if added_senders:
        changes.append(f"new people can trigger it: {', '.join(sorted(added_senders))}")
    if removed_senders:
        changes.append(f"no longer triggered by: {', '.join(sorted(removed_senders))}")
    if old.trigger.schedule != new.trigger.schedule:
        changes.append(f"schedule changed from {old.trigger.schedule} to {new.trigger.schedule}")

    before, after = _step_wire(old), _step_wire(new)
    for step_id in [s.step_id for s in new.steps if s.step_id not in before]:
        step = after[step_id]
        changes.append(f"added step {step_id} ({step['effect'].replace('_', ' ')} via {step['capability']}"
                       + (f" on {step['target']['kind']}:{step['target']['value']}" if step["target"] else "") + ")")
    for step_id in [s.step_id for s in old.steps if s.step_id not in after]:
        changes.append(f"removed step {step_id}")
    for step_id in [s.step_id for s in new.steps if s.step_id in before]:
        a, b = before[step_id], after[step_id]
        for field, label in (("capability", "capability"), ("target", "target"), ("effect", "effect"),
                             ("inputs", "inputs"), ("when", "condition"), ("verify", "checks"), ("wait", "wait")):
            if a.get(field) != b.get(field):
                changes.append(f"step {step_id}: {label} changed")

    for target in sorted(str(t) for t in new.targets - old.targets):
        changes.append(f"can now write to {target}")
    for target in sorted(str(t) for t in old.targets - new.targets):
        changes.append(f"no longer touches {target}")
    if old.budgets != new.budgets:
        changes.append(f"limits changed: {old.budgets.max_writes_per_run}→{new.budgets.max_writes_per_run} writes "
                       f"per run, {old.budgets.max_runs_per_day}→{new.budgets.max_runs_per_day} runs per day")
    if old.approval != new.approval:
        changes.append("approval rules changed")
    if not changes and old.pins != new.pins:
        changes.append("same steps; rebuilt against updated capabilities")
    return changes
