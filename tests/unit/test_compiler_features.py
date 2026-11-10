from __future__ import annotations

from capalis.core.expressions import FirstOf
from capalis.core.model import EvidenceLevel, StepKind, Target, TriggerKind
from support import OWNER, REPO, codes, compile_draft, hero_draft, manual_trigger, with_deploy_wait


def test_wait_step_polls_a_read_only_capability() -> None:
    result = compile_draft(with_deploy_wait(hero_draft()))
    assert result.ok, result.problems
    assert result.recipe is not None
    wait = result.recipe.step("deployed")
    assert wait is not None and wait.kind is StepKind.WAIT and wait.wait is not None
    assert wait.wait.poll_interval_s == 300
    assert Target("http.origin", "https://shop.example.com") in result.recipe.targets


def test_wait_step_requires_condition_and_deadline() -> None:
    draft = with_deploy_wait(hero_draft())
    draft["steps"][-1]["wait"].pop("deadline_s")
    assert "E_WAIT" in codes(compile_draft(draft))
    draft = with_deploy_wait(hero_draft())
    draft["steps"][-1]["wait"]["poll_interval_s"] = 5
    assert "E_WAIT" in codes(compile_draft(draft))


def test_write_steps_cannot_be_waits() -> None:
    draft = hero_draft()
    draft["steps"][1]["kind"] = "wait"
    assert "E_WAIT_NOT_READ" in codes(compile_draft(draft))


def test_v2_contract_observes_a_different_system() -> None:
    draft = hero_draft()
    draft["steps"][1]["verify"].append({
        "id": "site_mentions_fix", "probe": "capalis:http.get", "level": "V2", "deadline_s": 3600,
        "inputs": {"origin": {"const": "https://shop.example.com"}, "path": {"const": "/status"}},
        "expect": [{"path": "status", "op": "equals", "value": {"const": 200}}],
    })
    result = compile_draft(draft)
    assert result.ok, result.problems
    assert result.recipe is not None
    create = result.recipe.step("create")
    assert create is not None
    assert [c.level for c in create.verify] == [EvidenceLevel.V1, EvidenceLevel.V2]


def test_first_of_links_existing_or_new_issue() -> None:
    draft = hero_draft()
    draft["steps"][2]["inputs"]["text"] = {"first_of": ["steps.create.html_url", "steps.find.items.0.html_url"]}
    result = compile_draft(draft)
    assert result.ok, result.problems
    assert result.recipe is not None
    notify = result.recipe.step("notify")
    assert notify is not None and isinstance(notify.inputs["text"], FirstOf)
    assert set(notify.depends_on) == {"create", "find"}


def test_first_of_options_must_be_type_compatible() -> None:
    draft = hero_draft()
    draft["steps"][2]["inputs"]["text"] = {"first_of": ["steps.create.html_url", "steps.find.total_count"]}
    assert "E_SCHEMA_MISMATCH" in codes(compile_draft(draft))


def test_manual_trigger_runs_only_for_the_owner() -> None:
    result = compile_draft(manual_trigger(hero_draft()))
    assert result.ok, result.problems
    assert result.recipe is not None
    assert result.recipe.trigger.kind is TriggerKind.MANUAL
    assert result.recipe.trigger.senders == {OWNER}
    assert "E_TRIGGER_UNSCOPED" in codes(compile_draft(manual_trigger(hero_draft(), senders=["someone-else"])))


def test_schedule_trigger_needs_a_cron_expression() -> None:
    assert compile_draft(manual_trigger(hero_draft(), kind="schedule", schedule="0 9 * * 1-5")).ok
    bad = compile_draft(manual_trigger(hero_draft(), kind="schedule", schedule="every morning"))
    assert "E_TRIGGER_SCHEDULE" in codes(bad)


def test_retry_bounds_and_safe_retry_with_reconciliation() -> None:
    draft = hero_draft()
    draft["steps"][1]["retry"] = {"max_attempts": 3, "backoff_s": 30}
    assert compile_draft(draft).ok
    draft["steps"][1]["retry"] = {"max_attempts": 99}
    assert "E_RETRY" in codes(compile_draft(draft))


def test_contract_structure_is_validated() -> None:
    cases = [
        {"id": "Bad Id"},
        {"expect": []},
        {"expect": [{"path": "nope", "op": "exists"}]},
        {"expect": [{"path": "state", "op": "matches", "value": {"const": "x"}}]},
        {"expect": [{"path": "state", "op": "equals"}]},
        {"deadline_s": 0},
    ]
    for change in cases:
        draft = hero_draft()
        draft["steps"][1]["verify"][0].update(change)
        result = compile_draft(draft)
        assert not result.ok, change
    draft = hero_draft()
    draft["steps"][1]["verify"].append(dict(draft["steps"][1]["verify"][0]))
    assert "E_VERIFY" in codes(compile_draft(draft))


def test_unverifiable_reason_is_an_explicit_honest_alternative() -> None:
    draft = hero_draft()
    notify = draft["steps"][2]
    notify.pop("verify")
    notify["unverifiable_reason"] = "the Slack workspace blocks message history reads for this bot"
    result = compile_draft(draft)
    assert result.ok, result.problems
    assert result.recipe is not None
    step = result.recipe.step("notify")
    assert step is not None and step.verify == () and step.unverifiable_reason
    assert REPO in {t.value for t in result.recipe.targets}
