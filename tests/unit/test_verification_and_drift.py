from __future__ import annotations

from dataclasses import replace

import pytest

from capalis.core.drift import DriftKind, detect_drift
from capalis.core.model import Availability, EvidenceLevel, OutcomeContract, RecipeSpec, Target, Trust
from capalis.core.verification import (
    ContractStatus,
    HumanConfirmation,
    Observation,
    OutcomeStatus,
    evaluate_contract,
    summarize,
)
from support import REPO, compile_draft, hero_draft, registry

NOW = 1_800_000_000


@pytest.fixture
def recipe() -> RecipeSpec:
    compiled = compile_draft(hero_draft()).recipe
    assert compiled is not None
    return compiled


@pytest.fixture
def contract(recipe: RecipeSpec) -> OutcomeContract:
    create = recipe.step("create")
    assert create is not None
    return create.verify[0]


SCOPE = {"trigger": {"message_id": "m-42", "author": "ada", "text": "Checkout fails"},
         "steps": {"create": {"number": 7, "html_url": "https://github.com/x/7"}}}


def observation(**overrides: object) -> Observation:
    values: dict[str, object] = dict(
        contract_id="issue_readback", probe_id="capalis:github.issue.get", target=Target("github.repo", REPO),
        level=EvidenceLevel.V1, observed_at=NOW - 5,
        values={"number": 7, "state": "open", "body": "Checkout fails\n\nDiscord message m-42",
                "title": "Bug report from ada", "html_url": "https://github.com/x/7"},
    )
    values.update(overrides)
    return Observation(**values)  # type: ignore[arg-type]


def verdict(contract: OutcomeContract, obs: Observation | None, now: int = NOW) -> ContractStatus:
    return evaluate_contract(contract, obs, now=now, started_at=NOW - 30, scope=SCOPE).status


def test_independent_matching_readback_is_met(contract: OutcomeContract) -> None:
    assert verdict(contract, observation()) is ContractStatus.MET


def test_tool_reported_success_is_never_enough(contract: OutcomeContract) -> None:
    assert verdict(contract, observation(level=EvidenceLevel.V0)) is ContractStatus.MISSING


def test_observation_from_another_probe_or_target_is_rejected(contract: OutcomeContract) -> None:
    assert verdict(contract, observation(probe_id="capalis:github.issue.create")) is ContractStatus.ERROR
    assert verdict(contract, observation(target=Target("github.repo", "other/repo"))) is ContractStatus.ERROR


def test_wrong_state_is_not_met(contract: OutcomeContract) -> None:
    closed = observation(values={"state": "closed", "body": "Discord message m-42"})
    assert verdict(contract, closed) is ContractStatus.NOT_MET
    wrong_report = observation(values={"state": "open", "body": "someone else's report"})
    assert verdict(contract, wrong_report) is ContractStatus.NOT_MET


def test_stale_pending_and_missing(contract: OutcomeContract) -> None:
    assert verdict(contract, observation(observed_at=NOW - 10_000)) is ContractStatus.STALE
    assert verdict(contract, None, now=NOW) is ContractStatus.PENDING
    assert verdict(contract, None, now=NOW + 10_000) is ContractStatus.MISSING
    assert verdict(contract, observation(error="HTTP 502")) is ContractStatus.ERROR


def test_outcome_summary_is_conservative(contract: OutcomeContract) -> None:
    met = evaluate_contract(contract, observation(), now=NOW, started_at=NOW - 30, scope=SCOPE)
    missing = evaluate_contract(contract, None, now=NOW + 10_000, started_at=NOW - 30, scope=SCOPE)
    failed = evaluate_contract(contract, observation(values={"state": "closed", "body": "x"}),
                               now=NOW, started_at=NOW - 30, scope=SCOPE)
    assert summarize([met]).status is OutcomeStatus.VERIFIED
    assert summarize([met, missing]).status is OutcomeStatus.UNVERIFIED
    assert summarize([met, failed]).status is OutcomeStatus.FAILED
    assert summarize([]).status is OutcomeStatus.UNVERIFIED


def test_human_confirmation_is_a_separate_axis(contract: OutcomeContract) -> None:
    missing = evaluate_contract(contract, None, now=NOW + 10_000, started_at=NOW - 30, scope=SCOPE)
    outcome = summarize([missing], [HumanConfirmation("reporter", "looks fixed", NOW)])
    assert outcome.status is OutcomeStatus.UNVERIFIED
    assert outcome.human_confirmed


def test_no_drift_when_registry_unchanged(recipe: RecipeSpec) -> None:
    report = detect_drift(recipe, registry())
    assert report.changes == () and not report.blocking


@pytest.mark.parametrize(("change", "kind"), [
    ({"input_schema": {"type": "object", "properties": {"channel": {"type": "string"}, "text": {"type": "string"},
                                                        "blocks": {"type": "array"}},
                       "required": ["channel", "text"]}}, DriftKind.SCHEMA_CHANGED),
    ({"version": "2.0.0"}, DriftKind.VERSION_CHANGED),
    ({"trust": Trust.UNVERIFIED}, DriftKind.TRUST_LOWERED),
    ({"availability": Availability.NEEDS_AUTH}, DriftKind.UNAVAILABLE),
    ({"auth_requirement": "SLACK_USER_TOKEN"}, DriftKind.AUTH_CHANGED),
])
def test_drift_is_detected_and_blocking(recipe: RecipeSpec, change: dict[str, object], kind: DriftKind) -> None:
    caps = registry()
    caps["capalis:slack.message.post"] = replace(caps["capalis:slack.message.post"], **change)
    report = detect_drift(recipe, caps)
    assert report.blocking
    assert [c.kind for c in report.changes] == [kind]


def test_missing_capability_is_drift(recipe: RecipeSpec) -> None:
    caps = registry()
    del caps["capalis:github.issue.get"]
    report = detect_drift(recipe, caps)
    assert [(c.capability_id, c.kind) for c in report.changes] == [("capalis:github.issue.get", DriftKind.MISSING)]
