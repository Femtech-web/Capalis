from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from capalis.core.expressions import Const, Template
from capalis.core.model import (
    Availability,
    Effect,
    EvidenceLevel,
    IdempotencyStrategy,
    Target,
)
from support import DISCORD, REPO, SLACK, codes, compile_draft, hero_draft, registry


def test_hero_recipe_compiles_with_exact_scope() -> None:
    result = compile_draft(hero_draft())

    assert result.ok, result.problems
    recipe = result.recipe
    assert recipe is not None
    assert [step.step_id for step in recipe.steps] == ["find", "create", "notify"]
    assert recipe.targets == {Target("github.repo", REPO), Target("slack.channel", SLACK)}
    assert recipe.effects == {Effect.READ, Effect.EXTERNAL_WRITE, Effect.EXTERNAL_SEND}
    assert recipe.trigger.source == f"discord:channel:{DISCORD}"
    create = recipe.step("create")
    assert create is not None
    assert create.idempotency is IdempotencyStrategy.RECONCILE
    assert create.depends_on == ("find",)
    assert create.verify[0].level is EvidenceLevel.V1
    assert set(recipe.pins) == {
        "capalis:github.issue.search", "capalis:github.issue.create", "capalis:github.issue.find_by_key",
        "capalis:github.issue.get", "capalis:slack.message.post", "capalis:slack.message.find_by_key",
        "capalis:slack.message.get",
    }
    assert result.setup_needed == ()


def test_digest_is_deterministic_and_version_bound() -> None:
    first = compile_draft(hero_draft()).recipe
    second = compile_draft(hero_draft()).recipe
    bumped = compile_draft(hero_draft(), version=2).recipe
    assert first is not None and second is not None and bumped is not None
    assert first.digest == second.digest
    assert first.digest.startswith("sha256:")
    assert bumped.digest != first.digest


def test_unavailable_capability_compiles_but_reports_setup() -> None:
    caps = registry()
    caps["capalis:slack.message.post"] = _replace(caps["capalis:slack.message.post"],
                                                  availability=Availability.NEEDS_AUTH)
    result = compile_draft(hero_draft(), caps=caps)
    assert result.ok
    assert [item.capability_id for item in result.setup_needed] == ["capalis:slack.message.post"]


def _replace(cap: Any, **changes: Any) -> Any:
    from dataclasses import replace

    return replace(cap, **changes)


def _step(draft: dict[str, Any], step_id: str) -> dict[str, Any]:
    return next(step for step in draft["steps"] if step["id"] == step_id)


Mutation = Callable[[dict[str, Any]], None]

REJECTIONS: list[tuple[str, Mutation]] = [
    ("E_UNKNOWN_FIELD", lambda d: d.update(run_shell="rm -rf /")),
    ("E_UNKNOWN_CAPABILITY", lambda d: _step(d, "find").update(capability="capalis:github.issue.purge")),
    ("E_FORBIDDEN_CAPABILITY", lambda d: d["steps"].append(
        {"id": "shell", "capability": "mcp:shell.exec", "inputs": {"command": {"const": "ls"}},
         "unverifiable_reason": "shell output cannot be verified independently"})),
    ("E_UNCHARACTERIZED_EFFECT", lambda d: d["steps"].append(
        {"id": "page", "capability": "mcp:notion.page.create",
         "inputs": {"workspace": {"const": "acme"}, "title": {"const": "x"}},
         "unverifiable_reason": "no trusted verifier exists for this tool"})),
    ("E_RUNTIME_TARGET", lambda d: _step(d, "create")["inputs"].update(repo={"from": "trigger.text"})),
    ("E_RUNTIME_TARGET", lambda d: _step(d, "notify")["inputs"].update(channel={"template": "{trigger.text}"})),
    ("E_BAD_TARGET", lambda d: _step(d, "create")["inputs"].update(repo={"const": "not a repo"})),
    ("E_UNKNOWN_INPUT", lambda d: _step(d, "create")["inputs"].update(assignee={"const": "mallory"})),
    ("E_MISSING_INPUT", lambda d: _step(d, "create")["inputs"].pop("title")),
    ("E_SCHEMA_MISMATCH", lambda d: _step(d, "create")["inputs"].update(title={"const": 42})),
    ("E_SCHEMA_MISMATCH", lambda d: _step(d, "create")["inputs"].update(title={"from": "steps.find.total_count"})),
    ("E_BAD_REF", lambda d: _step(d, "create")["inputs"].update(body={"from": "trigger.secret_field"})),
    ("E_BAD_REF", lambda d: _step(d, "create")["inputs"].update(body={"from": "steps.ghost.body"})),
    ("E_BAD_REF", lambda d: _step(d, "create")["inputs"].update(body={"from": "steps.find.nope"})),
    ("E_SELF_REF", lambda d: _step(d, "create")["inputs"].update(body={"from": "steps.create.body"})),
    ("E_CYCLE", lambda d: _step(d, "find").update(after=["notify"])),
    ("E_DUPLICATE_STEP", lambda d: d["steps"].append(dict(_step(d, "find")))),
    ("E_UNSAFE_RETRY", lambda d: _step(d, "create").update(idempotency="none",
                                                          retry={"max_attempts": 3, "backoff_s": 5})),
    ("E_IDEMPOTENCY", lambda d: _step(d, "create").update(idempotency="native_key")),
    ("E_MISSING_VERIFIER", lambda d: _step(d, "notify").pop("verify")),
    ("E_PROBE_NOT_INDEPENDENT", lambda d: _step(d, "notify")["verify"][0].update(
        probe="capalis:slack.message.post")),
    ("E_PROBE_NOT_READONLY", lambda d: _step(d, "notify")["verify"][0].update(
        probe="capalis:github.issue.create")),
    ("E_VERIFIER_TARGET_MISMATCH", lambda d: _step(d, "create")["verify"][0]["inputs"].update(
        repo={"const": "someone-else/repo"})),
    ("E_V2_NOT_CROSS_SYSTEM", lambda d: _step(d, "create")["verify"][0].update(level="V2")),
    ("E_BAD_LEVEL", lambda d: _step(d, "create")["verify"][0].update(level="V0")),
    ("E_EFFECT_MISMATCH", lambda d: _step(d, "create").update(effect="read")),
    ("E_VERIFY_ON_READ", lambda d: _step(d, "find").update(unverifiable_reason="reads are not verified here")),
    ("E_TRIGGER_UNSCOPED", lambda d: d["trigger"].update(senders=["*"])),
    ("E_TRIGGER_UNSCOPED", lambda d: d["trigger"].pop("senders")),
    ("E_TRIGGER_NO_DEDUPE", lambda d: d["trigger"].pop("dedupe_key")),
    ("E_TRIGGER_NO_DEDUPE", lambda d: d["trigger"].update(dedupe_key="missing_field")),
    ("E_TRIGGER_SOURCE", lambda d: d["trigger"].update(source="anywhere")),
    ("E_PAYLOAD_SCHEMA", lambda d: d["trigger"]["payload_schema"].update(additionalProperties=True)),
    ("E_SCHEMA_UNSUPPORTED", lambda d: d["trigger"]["payload_schema"].update(anyOf=[{"type": "string"}])),
    ("E_BUDGET_TOO_SMALL", lambda d: d["budgets"].update(max_writes_per_run=1)),
    ("E_BAD_CONDITION", lambda d: _step(d, "create").update(when={"op": "equals", "ref": "steps.find.total_count",
                                                                  "value": {"nested": "object"}})),
    ("E_STEP_ID", lambda d: _step(d, "find").update(id="Find Issues")),
    ("E_INTENT", lambda d: d.update(intent="")),
]


@pytest.mark.parametrize(("code", "mutate"), REJECTIONS, ids=[f"{c}-{i}" for i, (c, _) in enumerate(REJECTIONS)])
def test_compiler_rejects(code: str, mutate: Mutation) -> None:
    draft = hero_draft()
    mutate(draft)
    result = compile_draft(draft)
    assert not result.ok
    assert result.recipe is None
    assert code in codes(result), result.problems


def test_delete_effect_requires_per_run_confirmation() -> None:
    draft = hero_draft()
    draft["steps"].append({
        "id": "cleanup", "capability": "capalis:github.issue.delete", "after": ["notify"],
        "inputs": {"repo": {"const": REPO}, "number": {"from": "steps.create.number"}},
        "idempotency": "none",
        "unverifiable_reason": "deletion is checked by a later read in a separate recipe",
    })
    draft["approval"] = {"per_run_confirmation": []}
    assert "E_CONFIRMATION_REQUIRED" in codes(compile_draft(draft))

    draft["approval"] = {"per_run_confirmation": ["delete_or_revoke", "spend"]}
    assert compile_draft(draft).ok


def test_template_inputs_are_typed_as_text_and_targets_stay_constant() -> None:
    recipe = compile_draft(hero_draft()).recipe
    assert recipe is not None
    create = recipe.step("create")
    assert create is not None
    assert isinstance(create.inputs["repo"], Const)
    assert isinstance(create.inputs["title"], Template)
    assert create.target == Target("github.repo", REPO)


def test_draft_is_not_mutated_or_trusted_for_effects() -> None:
    draft = hero_draft()
    _step(draft, "create")["effect"] = "external_write"
    before = repr(draft)
    result = compile_draft(draft)
    assert result.ok
    assert repr(draft) == before
