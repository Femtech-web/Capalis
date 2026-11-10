from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from capalis.adapters import surfaces
from capalis.core.lifecycle import RecipeState, RunState
from capalis.core.redact import Redactor
from capalis.service import Actor, Capalis, ServiceError
from capalis.storage.db import Store
from fakes import World, adapters
from support import DISCORD, REPO, REPORTER, SLACK

LOCAL = Actor("cli", None)
STRANGER = Actor("telegram", "555")
OWNER_TG = Actor("telegram", "1001")
SAMPLE = {"message_id": "1569000000000000001", "author": "reporter", "text": "Checkout button does nothing"}


class Env:
    def __init__(self, path: Path) -> None:
        self.world = World()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.store = Store(self.conn, redactor=Redactor())
        self.service = Capalis(self.store, adapters(self.world), approvers=lambda: ["telegram:1001"])
        self.tools = surfaces.handlers(lambda: self.service)
        self.command = surfaces.command(lambda: self.service)

    def tool(self, name: str, **args: Any) -> Any:
        return json.loads(self.tools[name](args))

    def slash(self, text: str, actor: Actor = LOCAL) -> str:
        source = None if actor.user_id is None else type("Src", (), {"user_id": actor.user_id})()
        return self.command(text, platform=actor.platform, source=source)

    def draft(self, *, reply_when_closed: bool = False) -> dict[str, Any]:
        out = self.tool("capalis_recipe", action="template", repo=REPO, slack_channel=SLACK,
                        discord_channel=DISCORD, reporters=[REPORTER], reply_when_closed=reply_when_closed)
        return dict(out["draft"])

    def wait_for(self, run_id: str, timeout: float = 5) -> RunState:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            state = self.store.run(run_id).state
            if state not in (RunState.QUEUED, RunState.RUNNING):
                return state
            time.sleep(0.02)
        return self.store.run(run_id).state

    def message(self, message_id: str = "1569637681166811227", sender: str = REPORTER, chat: str = DISCORD) -> Any:
        return self.service.on_message(platform="discord", chat_id=chat, sender=sender, message_id=message_id,
                                       author="reporter", text="Checkout is broken")


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    e = Env(tmp_path / "svc.db")
    yield e
    e.service.stop()
    e.conn.close()


def qualified(env: Env, *, reply_when_closed: bool = False) -> str:
    compiled = env.tool("capalis_recipe", action="compile", recipe_id="bug-reports",
                        draft=env.draft(reply_when_closed=reply_when_closed))
    assert compiled["ok"], compiled
    result = env.tool("capalis_recipe", action="qualify", recipe_id="bug-reports", sample_payload=SAMPLE)
    assert result["qualified"] and result["state"] == "awaiting_approval", result
    return str(compiled["short_digest"])


def test_compile_qualify_approve_then_live_intake(env: Env) -> None:
    digest = qualified(env)
    assert env.world.writes == [], "qualification must not write"
    assert "not an approver" in env.slash(f"approve bug-reports {digest}", STRANGER)
    assert "does not match" in env.slash("approve bug-reports deadbeef00", LOCAL)
    assert "Approved bug-reports v1" in env.slash(f"approve bug-reports {digest}", OWNER_TG)

    sub = env.message("1569000000000000100")
    assert sub is not None and sub.accepted and sub.run_id is not None
    assert env.wait_for(sub.run_id) is RunState.VERIFIED
    evidence = env.tool("capalis_run", action="evidence", run_id=sub.run_id)
    assert evidence["state"] == "verified" and evidence["event_log_intact"]
    assert {c["status"] for c in evidence["independent_checks"]} == {"met"}
    assert len(env.world.issues[REPO]) == 1


def test_events_before_approval_run_in_shadow(env: Env) -> None:
    qualified(env)
    sub = env.message("1569000000000000001")
    assert sub is not None and sub.run_id is not None
    assert env.wait_for(sub.run_id) is RunState.UNVERIFIED
    assert env.store.run(sub.run_id).mode.value == "shadow" and env.world.writes == []


def test_unrelated_channels_and_senders(env: Env) -> None:
    digest = qualified(env)
    env.slash(f"approve bug-reports {digest}")
    assert env.message("1569000000000000001", chat="999999999999999999") is None
    rejected = env.message("1569000000000000002", sender="intruder")
    assert rejected is not None and not rejected.accepted and rejected.code == "unauthorized_sender"
    assert env.world.writes == []


def test_bad_draft_reports_problems_and_stores_nothing(env: Env) -> None:
    draft = env.draft()
    draft["steps"][1]["inputs"]["repo"] = {"from": "trigger.text"}
    out = env.tool("capalis_recipe", action="compile", recipe_id="bug-reports", draft=draft)
    assert not out["ok"] and "E_RUNTIME_TARGET" in {p["code"] for p in out["problems"]}
    assert env.tool("capalis_recipe", action="list") == []


def test_recompiling_is_idempotent_and_a_new_version_does_not_displace_the_live_one(env: Env) -> None:
    digest = qualified(env)
    env.slash(f"approve bug-reports {digest}")
    again = env.tool("capalis_recipe", action="compile", recipe_id="bug-reports", draft=env.draft())
    assert again.get("unchanged") and again["version"] == 1
    changed = env.draft()
    changed["budgets"]["max_runs_per_day"] = 50
    v2 = env.tool("capalis_recipe", action="compile", recipe_id="bug-reports", draft=changed)
    assert v2["ok"] and v2["version"] == 2 and v2["state"] == "compiled"
    sub = env.message("1569000000000000003")
    assert sub is not None and sub.run_id is not None and env.wait_for(sub.run_id) is RunState.VERIFIED
    assert env.store.run(sub.run_id).recipe_digest.startswith("sha256:" + digest)

    env.tool("capalis_recipe", action="qualify", recipe_id="bug-reports", sample_payload=SAMPLE)
    assert "Approved bug-reports v2" in env.slash(f"approve bug-reports {v2['short_digest']}")
    states = {version: state for _, version, _, state in env.store.recipes()}
    assert states == {1: RecipeState.RETIRED, 2: RecipeState.LIVE}


def test_model_can_pause_but_only_the_owner_can_resume(env: Env) -> None:
    env.slash(f"approve bug-reports {qualified(env)}")
    assert "paused" in env.tool("capalis_pause", recipe_id="bug-reports")["result"]
    held = env.message("1569000000000000004")
    assert held is not None and not held.accepted and held.code == "state"
    assert "not an approver" in env.slash("resume bug-reports", STRANGER)
    assert "live again" in env.slash("resume bug-reports", OWNER_TG)
    assert env.tool("capalis_pause")["result"].startswith("All Capalis Recipes are paused")
    assert "running again" in env.slash("resume", LOCAL)


def test_revoke_returns_the_version_to_awaiting_approval(env: Env) -> None:
    env.slash(f"approve bug-reports {qualified(env)}")
    assert "revoked" in env.slash("revoke bug-reports")
    assert env.store.latest("bug-reports")[1] is RecipeState.AWAITING_APPROVAL  # type: ignore[index]
    sub = env.message("1569000000000000005")
    assert sub is not None and env.store.run(sub.run_id or "").mode.value == "shadow"


def test_compare_explains_why_the_webhook_cannot_be_recommended(env: Env) -> None:
    [match] = env.tool("capalis_capabilities", needs=[
        {"need_id": "notify", "operation": "message.post", "max_effect": "external_send"}])
    assert match["recommended"] == "capalis:slack.message.post"
    webhook = next(c for c in match["candidates"] if c["id"] == "capalis:slack.webhook.post")
    assert not webhook["can_run"] and "V1" in webhook["excluded_because"][0]
    [relaxed] = env.tool("capalis_capabilities", needs=[
        {"need_id": "notify", "operation": "message.post", "max_effect": "external_send", "min_evidence": "none"}])
    assert relaxed["recommended"] == "capalis:slack.message.post"
    assert all(c["can_run"] for c in relaxed["candidates"])
    assert "verification" in relaxed["rationale"]


def test_tool_errors_are_json_not_exceptions(env: Env) -> None:
    assert "error" in env.tool("capalis_recipe", action="qualify", recipe_id="nope")
    assert "error" in env.tool("capalis_run", action="evidence", run_id="nope")
    assert "error" in env.tool("capalis_recipe", action="compile", recipe_id="Bad Id", draft={})
    with pytest.raises(ServiceError):
        env.service.approve("bug-reports", "x", STRANGER)


def test_gateway_hook_consumes_trigger_messages_and_ignores_the_rest(env: Env) -> None:
    from types import SimpleNamespace

    from capalis.hermes import SKIP, ServiceHolder

    qualified(env)
    holder = ServiceHolder(ctx=None, factory=lambda: env.service)

    def event(chat: str, user: str = REPORTER, *, bot: bool = False, internal: bool = False) -> Any:
        source = SimpleNamespace(platform=SimpleNamespace(value="discord"), chat_id=chat, user_id=user,
                                 user_name="reporter", is_bot=bot, message_id=None)
        return SimpleNamespace(source=source, text="Checkout is broken", message_id="m-77", internal=internal)

    assert holder.on_gateway_message(event=event(DISCORD)) == SKIP
    assert holder.on_gateway_message(event=event("999999999999999999")) is None
    assert holder.on_gateway_message(event=event(DISCORD, bot=True)) is None
    assert holder.on_gateway_message(event=event(DISCORD, internal=True)) is None
    assert holder.on_gateway_message(event=SimpleNamespace(source=None)) is None
    broken = ServiceHolder(ctx=None, factory=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert broken.on_gateway_message(event=event(DISCORD)) is None


def test_drift_sweep_quarantines_without_waiting_for_a_trigger(env: Env) -> None:
    from dataclasses import replace

    from capalis.core.model import Availability
    from fakes import FakeAdapter

    env.slash(f"approve bug-reports {qualified(env)}")
    post = env.service.adapters["capalis:slack.message.post"]
    env.service.adapters["capalis:slack.message.post"] = FakeAdapter(  # type: ignore[index]
        env.world, replace(post.descriptor, availability=Availability.NEEDS_AUTH))
    [hit] = env.service.drift_sweep()
    assert hit["recipe_id"] == "bug-reports" and "unavailable" in hit["reason"]
    status = env.tool("capalis_status")
    assert status["quarantined"][0]["reason"] == hit["reason"]
    assert env.service.drift_sweep() == []
    rejected = env.message("1569000000000000009")
    assert rejected is None, "a quarantined Recipe no longer listens"
    assert env.world.writes == []


def test_reporter_hears_back_when_the_issue_is_closed(env: Env) -> None:
    digest = qualified(env, reply_when_closed=True)
    shadow = env.store.runs()[0]
    assert {a.step_id: a.state.value for a in env.store.latest_attempts(shadow.run_id).values()} == {
        "find": "completed", "create": "simulated", "notify": "simulated", "ack": "simulated", "fixed": "simulated",
        "reply_fixed": "skipped", "reply_closed": "simulated"}
    assert env.world.writes == []
    env.slash(f"approve bug-reports {digest}")

    sub = env.message("1569637681166811227")
    assert sub is not None and sub.run_id is not None
    assert env.wait_for(sub.run_id) is RunState.WAITING
    assert env.store.latest_attempts(sub.run_id)["fixed"].state.value == "waiting"
    [ack] = env.world.discord[DISCORD]
    assert ack["reply_to"] == "1569637681166811227" and "tracked as https://github.com/" in ack["text"]

    env.world.close_issue(REPO, 1)
    assert env.service.runner.advance(sub.run_id) is RunState.VERIFIED
    _, reply = env.world.discord[DISCORD]
    assert reply["reply_to"] == "1569637681166811227" and "has been fixed and closed" in reply["text"]
    steps = {a.step_id: a.state.value for a in env.store.latest_attempts(sub.run_id).values()}
    assert steps["reply_fixed"] == "observed_verified" and steps["reply_closed"] == "skipped"
    assert {c["check"]: c["status"] for c in env.tool("capalis_run", action="evidence", run_id=sub.run_id)[
        "independent_checks"]} == {"issue_readback": "met", "message_readback": "met", "ack_readback": "met",
                                   "reply_fixed_readback": "met"}


def test_closed_without_a_fix_says_so(env: Env) -> None:
    env.slash(f"approve bug-reports {qualified(env, reply_when_closed=True)}")
    sub = env.message("1569637681166811230")
    assert sub is not None and sub.run_id is not None and env.wait_for(sub.run_id) is RunState.WAITING
    env.world.close_issue(REPO, 1, reason="not_planned")
    assert env.service.runner.advance(sub.run_id) is RunState.VERIFIED
    assert "closed without a fix (reason: not_planned)" in env.world.discord[DISCORD][-1]["text"]


def test_ambiguous_discord_reply_is_found_by_its_text_not_resent(env: Env) -> None:
    env.slash(f"approve bug-reports {qualified(env, reply_when_closed=True)}")
    env.world.fault("capalis:discord.message.post", "ambiguous_after")
    sub = env.message("1569637681166811228")
    assert sub is not None and sub.run_id is not None and env.wait_for(sub.run_id) is RunState.WAITING
    assert len(env.world.discord[DISCORD]) == 1, "the acknowledgement was posted once despite the timeout"
    env.world.close_issue(REPO, 1)
    assert env.service.runner.advance(sub.run_id) is RunState.VERIFIED
    assert len(env.world.discord[DISCORD]) == 2
    assert len(env.world.calls_to("capalis:discord.message.post")) == 2


def test_unrealistic_sample_payload_is_rejected_with_a_reason(env: Env) -> None:
    env.tool("capalis_recipe", action="compile", recipe_id="bug-reports", draft=env.draft(reply_when_closed=True))
    result = env.tool("capalis_recipe", action="qualify", recipe_id="bug-reports",
                      sample_payload={"message_id": "sample-1", "author": "r", "text": "t"})
    assert not result["qualified"] and result["code"] == "payload_invalid" and "message_id" in result["detail"]


def test_owner_views_are_readable_and_raw_json_is_available(env: Env) -> None:
    digest = qualified(env, reply_when_closed=True)
    show = env.slash("show bug-reports")
    assert f"To approve this exact version: /capalis approve bug-reports {digest}" in show
    assert "5. fixed         wait" in show and "steps.fixed.state equals closed" in show
    shadow = env.store.runs()[0]
    assert "4 write(s) simulated, nothing written; 1 later step(s) not run" in shadow.detail
    env.slash(f"approve bug-reports {digest}")
    sub = env.message("1569637681166811229")
    assert sub is not None and sub.run_id is not None and env.wait_for(sub.run_id) is RunState.WAITING
    env.world.close_issue(REPO, 1)
    env.service.runner.advance(sub.run_id)

    text = env.slash(f"evidence {sub.run_id[:8]}")
    assert text.startswith(f"Run {sub.run_id[:8]} - bug-reports - live - VERIFIED")
    assert "reply_fixed_readback  V1  met" in text and "Event log:" in text and "intact" in text
    assert json.loads(env.slash(f"evidence {sub.run_id[:8]} --json"))["state"] == "verified"
    assert "Needs your attention: nothing" in env.slash("")
    assert "no single run" in env.slash("evidence abc")


def test_owner_uses_names_and_approves_with_a_button(tmp_path: Path) -> None:
    from fakes import FakeDirectory

    env = Env(tmp_path / "names.db")
    env.service.directory = FakeDirectory(REPO, SLACK, DISCORD, REPORTER)
    asked: list[str] = []

    def ask(answer: str | None) -> Any:
        def picker(question: str, choices: list[Any]) -> str | None:
            asked.append(question)
            assert [c["label"] for c in choices] == ["Approve", "Not now"]
            return answer
        return picker

    try:
        out = env.tool("capalis_recipe", action="template", repo="storefront", slack_channel="#engineering",
                       discord_channel="#bugs", reporters=["@ada"])
        assert [(r["asked"], r["name"]) for r in out["resolved"]] == [
            ("storefront", REPO), ("#engineering", "#engineering"), ("#bugs", "#bugs"), ("@ada", "@ada")]
        assert "error" in env.tool("capalis_recipe", action="template", repo="storefront",
                                   slack_channel="#engineering", discord_channel="#bugs", reporters=["@bob"])
        env.tool("capalis_recipe", action="compile", recipe_id="bug-reports", draft=out["draft"])
        env.tool("capalis_recipe", action="qualify", recipe_id="bug-reports", sample_payload=SAMPLE)
        assert "#engineering (slack.channel:" in env.slash("show bug-reports")

        no = surfaces.command(lambda: env.service, ask=ask("cancel"))
        assert no("approve bug-reports", platform="cli") == "Not approved. Nothing changed."
        assert "When @ada posts in #bugs, it will open an issue in" in asked[0]
        assert "then post in #engineering" in asked[0]
        stranger = surfaces.command(lambda: env.service, ask=ask("approve"))
        source = type("Src", (), {"user_id": "555"})()
        assert "not an approver" in stranger("approve bug-reports", platform="telegram", source=source)
        assert len(asked) == 1, "no prompt for someone who cannot approve"
        assert "No buttons here" in surfaces.command(lambda: env.service)("approve bug-reports", platform="cli")
        yes = surfaces.command(lambda: env.service, ask=ask("approve"))
        assert yes("approve bug-reports", platform="cli").startswith("Approved bug-reports v1")
    finally:
        env.service.stop()
        env.conn.close()
