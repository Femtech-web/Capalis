from __future__ import annotations

from dataclasses import replace

from capalis.core.matching import Basis, ObservedStats, Requirement, match_goal, match_requirement
from capalis.core.model import Availability, Effect, EvidenceLevel
from support import registry

NOTIFY = Requirement("notify_engineering", "message.post", "Tell engineering in Slack", Effect.EXTERNAL_SEND,
                     min_evidence=None)
NOTIFY_VERIFIED = replace(NOTIFY, min_evidence=EvidenceLevel.V1)


def test_recommends_the_verifiable_option_and_names_the_tradeoff() -> None:
    match = match_requirement(NOTIFY, registry())
    ids = [c.capability_id for c in match.candidates]
    assert ids == ["capalis:slack.message.post", "capalis:slack.webhook.post"]
    assert all(c.executable for c in match.candidates)
    assert match.recommended == "capalis:slack.message.post"
    assert "verification" in match.rationale
    assert "capalis:slack.webhook.post: no independent read-back" in match.rationale
    assert "%" not in match.rationale


def test_required_evidence_excludes_unverifiable_options() -> None:
    match = match_requirement(NOTIFY_VERIFIED, registry())
    webhook = next(c for c in match.candidates if c.capability_id == "capalis:slack.webhook.post")
    assert not webhook.executable
    assert "requires V1 evidence" in webhook.exclusions[0]
    assert match.recommended == "capalis:slack.message.post"
    assert "only candidate" in match.rationale


def test_setup_cost_is_shown_but_does_not_outrank_verification() -> None:
    caps = registry()
    caps["capalis:slack.message.post"] = replace(caps["capalis:slack.message.post"],
                                                 availability=Availability.NEEDS_AUTH)
    match = match_requirement(NOTIFY, caps)
    bot = next(c for c in match.candidates if c.capability_id == "capalis:slack.message.post")
    assert bot.factor("setup").summary == "needs a sign-in or token"
    assert match.recommended == "capalis:slack.message.post"


def test_no_winner_when_evidence_does_not_separate_candidates() -> None:
    caps = registry()
    twin = replace(caps["capalis:slack.message.post"], id="capalis:slack.message.post_v2")
    caps[twin.id] = twin
    match = match_requirement(NOTIFY_VERIFIED, caps)
    assert match.recommended is None
    assert "compare them or test both" in match.rationale


def test_observed_reliability_breaks_ties_with_counts_not_percentages() -> None:
    caps = registry()
    twin = replace(caps["capalis:slack.message.post"], id="capalis:slack.message.post_v2")
    caps[twin.id] = twin
    observed = {twin.id: ObservedStats(verified=3, environment="sandbox")}
    match = match_requirement(NOTIFY_VERIFIED, caps, observed)
    assert match.recommended == twin.id
    reliability = match.candidates[0].factor("reliability")
    assert reliability.basis is Basis.OBSERVED
    assert reliability.summary == "3 verified, 0 failed, 0 unverified (sandbox)"
    untested = match.candidates[1].factor("reliability")
    assert (untested.basis, untested.summary) == (Basis.UNKNOWN, "no runs observed yet")


def test_untrusted_shell_and_over_risk_candidates_are_excluded() -> None:
    caps = registry()
    caps["mcp:notion.page.create"] = replace(caps["mcp:notion.page.create"], operation="message.post")
    caps["mcp:shell.exec"] = replace(caps["mcp:shell.exec"], operation="message.post")
    match = match_requirement(NOTIFY, caps)
    reasons = {c.capability_id: c.exclusions for c in match.candidates if not c.executable}
    assert "not characterized" in reasons["mcp:notion.page.create"][0]
    assert any("shell" in r for r in reasons["mcp:shell.exec"])
    read_only = replace(NOTIFY, max_effect=Effect.READ)
    assert match_requirement(read_only, registry()).recommended is None


def test_goal_level_decision_reports_gaps_honestly() -> None:
    goal = [Requirement("track", "issue.create", "Create the issue", Effect.EXTERNAL_WRITE),
            Requirement("page", "pagerduty.incident.create", "Page on-call", Effect.EXTERNAL_SEND)]
    decision = match_goal(goal, registry())
    assert decision.gaps == ("page",)
    assert decision.recommended is None
    page = decision.matches[1]
    assert page.candidates == () and "No known capability" in page.rationale


def test_goal_decision_is_deterministic() -> None:
    goal = [Requirement("track", "issue.create", "Create the issue", Effect.EXTERNAL_WRITE), NOTIFY]
    first, second = match_goal(goal, registry()), match_goal(goal, registry())
    assert first == second
    assert first.recommended == {"track": "capalis:github.issue.create",
                                 "notify_engineering": "capalis:slack.message.post"}
