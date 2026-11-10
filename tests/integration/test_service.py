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


def test_settings_are_read_with_the_hermes_get_config_signature() -> None:
    from capalis.hermes import ServiceHolder

    class Ctx:
        """v0.23.0: PluginContext.get_config(key, default) reads plugins.entries.capalis.settings.<key>."""

        def __init__(self, settings: dict[str, Any]) -> None:
            self.settings = settings

        def get_config(self, key: str, default: Any = None) -> Any:
            return self.settings.get(key, default)

    holder = ServiceHolder(ctx=Ctx({"approvers": ["telegram:1145236794"], "alert_channel": " telegram:1145236794 "}))
    assert holder._approvers() == ["telegram:1145236794"]
    assert holder._alert_channel() == "telegram:1145236794"
    empty = ServiceHolder(ctx=Ctx({}))
    assert list(empty._approvers()) == [] and empty._alert_channel() is None


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
    assert hit["recipe_id"] == "bug-reports" and hit["reason"].startswith("slack.message.post can't be used")
    [event] = [e for e in env.store.events() if e.payload.get("to") == "quarantined"]
    assert "capalis:slack.message.post: unavailable" in event.payload["detail"], "evidence keeps the detail"
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
    assert "Needs you\n• nothing" in env.slash("")
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
        assert "When @ada posts in #bugs, it will:\n• Open an issue in acme-labs/storefront" in asked[0]
        assert "• Post in #engineering" in asked[0] and "• Wait until the issue is closed" in asked[0]
        stranger = surfaces.command(lambda: env.service, ask=ask("approve"))
        source = type("Src", (), {"user_id": "555"})()
        assert "not an approver" in stranger("approve bug-reports", platform="telegram", source=source)
        assert len(asked) == 1, "no prompt for someone who cannot approve"
        assert "No buttons here" in surfaces.command(lambda: env.service)("approve bug-reports", platform="cli")
        yes = surfaces.command(lambda: env.service, ask=ask("approve"))
        assert yes("approve bug-reports", platform="cli").startswith("✅ Approved bug-reports v1 for 30 days.")
    finally:
        env.service.stop()
        env.conn.close()


def test_slack_intake_replies_in_the_reports_thread(tmp_path: Path) -> None:
    from fakes import FakeDirectory

    env = Env(tmp_path / "slack.db")
    env.service.directory = FakeDirectory(REPO, SLACK, DISCORD, REPORTER)
    try:
        out = env.tool("capalis_recipe", action="template", intake="slack", repo="storefront",
                       slack_channel="#engineering", discord_channel="#bugs", reporters=["@ada"])
        assert out["draft"]["trigger"]["source"] == "slack:channel:C0BUGS00001"
        compiled = env.tool("capalis_recipe", action="compile", recipe_id="slack-bugs", draft=out["draft"])
        assert compiled["ok"], compiled
        q = env.tool("capalis_recipe", action="qualify", recipe_id="slack-bugs",
                     sample_payload={"message_id": "1794300000.000100", "author": "ada", "text": "Login loops"})
        assert q["qualified"], q
        env.slash(f"approve slack-bugs {compiled['short_digest']}")
        sub = env.service.on_message(platform="slack", chat_id="C0BUGS00001", sender="U0ADA00001",
                                     message_id="1794300000.000200", author="ada", text="Login loops forever")
        assert sub is not None and sub.run_id is not None and env.wait_for(sub.run_id) is RunState.WAITING
        [ack] = env.world.messages["C0BUGS00001"]
        assert ack["thread_ts"] == "1794300000.000200" and "tracked as" in ack["text"]
        env.world.close_issue(REPO, 1)
        assert env.service.runner.advance(sub.run_id) is RunState.VERIFIED
        assert env.world.messages["C0BUGS00001"][-1]["thread_ts"] == "1794300000.000200"
    finally:
        env.service.stop()
        env.conn.close()


def test_replies_never_start_a_new_report(env: Env) -> None:
    from types import SimpleNamespace

    from capalis.hermes import ServiceHolder

    env.slash(f"approve bug-reports {qualified(env, reply_when_closed=True)}")
    holder = ServiceHolder(ctx=None, factory=lambda: env.service)
    source = SimpleNamespace(platform=SimpleNamespace(value="discord"), chat_id=DISCORD, user_id=REPORTER,
                             user_name="r", is_bot=False, thread_id=None)
    reply = SimpleNamespace(source=source, text="thanks!", message_id="1569000000000000777", internal=False,
                            reply_to_message_id="1569000000000000001")
    assert holder.on_gateway_message(event=reply) is None
    slack_thread = SimpleNamespace(source=SimpleNamespace(platform=SimpleNamespace(value="slack"), chat_id="C1",
                                                          user_id="U1", user_name="u", is_bot=False,
                                                          thread_id="1794300000.000200"),
                                   text="thanks", message_id="1794300000.000300", internal=False)
    assert holder.on_gateway_message(event=slack_thread) is None
    assert env.store.runs()[0].mode.value == "shadow", "no live run was started by the replies"


def test_owner_alerts_are_sent_once_from_now_on(tmp_path: Path) -> None:
    from dataclasses import replace

    from capalis.core.model import Availability
    from fakes import FakeAdapter, FakeDirectory

    env = Env(tmp_path / "alerts.db")
    env.service.directory = FakeDirectory(REPO, SLACK, DISCORD, REPORTER)
    channel = {"value": "slack:#alerts"}
    env.service.alert_channel = lambda: channel["value"]
    try:
        env.slash(f"approve bug-reports {qualified(env)}")
        assert env.service.send_alerts() == 0, "enabling alerts does not replay history"
        post = env.service.adapters["capalis:slack.message.post"]
        env.service.adapters["capalis:slack.message.post"] = FakeAdapter(  # type: ignore[index]
            env.world, replace(post.descriptor, availability=Availability.NEEDS_AUTH))
        env.service.drift_sweep()
        env.service.adapters["capalis:slack.message.post"] = post  # type: ignore[index]
        assert env.service.send_alerts() == 1
        [alert] = env.world.messages[SLACK]
        assert alert["text"].startswith("⛔ bug-reports v1 has stopped itself\n\nWhy: slack.message.post")
        assert "Nothing more will be written until you act." in alert["text"]
        assert 'say: "Rebuild the bug-reports Recipe"' in alert["text"]
        assert env.service.send_alerts() == 0, "each alert is sent once"

        env.world.fault("capalis:slack.message.post", "rejected")
        env.service.store.append_event("recipe.state", {"digest": env.store.recipes()[0][2], "to": "quarantined",
                                                        "reason": "test"})
        assert env.service.send_alerts() == 0
        assert env.service.send_alerts() == 1, "a failed delivery is retried, not skipped"
    finally:
        env.service.stop()
        env.conn.close()


def test_cancel_and_presentation_preference(env: Env) -> None:
    draft = env.draft()
    draft["approval"] = {"per_run_confirmation": ["external_write"]}
    env.tool("capalis_recipe", action="compile", recipe_id="bug-reports", draft=draft)
    env.tool("capalis_recipe", action="qualify", recipe_id="bug-reports", sample_payload=SAMPLE)
    env.slash(f"approve bug-reports {env.store.latest('bug-reports')[0].digest[7:19]}")  # type: ignore[index]
    sub = env.message("1569000000000000811")
    assert sub is not None and sub.run_id is not None and env.wait_for(sub.run_id) is RunState.WAITING
    assert "CANCELLED" in env.slash(f"cancel {sub.run_id[:8]}")
    assert env.world.writes == []
    assert "show the full comparison first" in env.slash("prefer compare")
    assert env.store.presentation("cli") == "compare"


def test_reliability_counts_come_from_this_installs_live_runs(env: Env) -> None:
    env.slash(f"approve bug-reports {qualified(env)}")
    sub = env.message("1569000000000000900")
    assert sub is not None and sub.run_id is not None and env.wait_for(sub.run_id) is RunState.VERIFIED
    [match] = env.tool("capalis_capabilities", needs=[
        {"need_id": "notify", "operation": "message.post", "max_effect": "external_send"}])
    bot = next(c for c in match["candidates"] if c["id"] == "capalis:slack.message.post")
    assert bot["factors"]["reliability"]["summary"].startswith("1 verified, 0 failed, 0 unverified")
    assert bot["factors"]["reliability"]["basis"] == "observed"


def test_site_watch_fires_on_schedule_and_alerts_once_per_slot(tmp_path: Path) -> None:
    import calendar

    from fakes import FakeDirectory

    env = Env(tmp_path / "watch.db")
    env.service.directory = FakeDirectory(REPO, SLACK, DISCORD, REPORTER)
    # One clock for the test, the approval and the background worker (which also fires schedules).
    t0 = calendar.timegm((2026, 11, 10, 12, 1, 0, 0, 0, 0))
    now = {"t": t0}
    env.store.clock = lambda: now["t"]
    try:
        out = env.tool("capalis_recipe", action="template", template="site_watch",
                       url="https://shop.example.com/health", slack_channel="#engineering", every_minutes=10)
        assert out["draft"]["trigger"] == {"kind": "schedule", "schedule": "*/10 * * * *", "max_runs_per_hour": 6}
        compiled = env.tool("capalis_recipe", action="compile", recipe_id="site-watch", draft=out["draft"])
        assert compiled["ok"], compiled
        assert env.tool("capalis_recipe", action="qualify", recipe_id="site-watch", sample_payload={})["qualified"]
        asked: list[str] = []
        surfaces.command(lambda: env.service, ask=lambda q, c: asked.append(q))("approve site-watch", platform="cli")
        assert "Every 10 minutes, it will:\n• Check shop.example.com/health\n• Post in #engineering" in asked[0]
        approved = env.slash(f"approve site-watch {compiled['short_digest']}")
        assert "It can only act in: #engineering, shop.example.com." in approved
        assert approved.endswith("Pause it any time: /capalis pause site-watch")
        assert "To pause one, send /capalis pause and its name, e.g. /capalis pause site-watch" in env.slash("")

        assert env.service.fire_schedules() == [], "counting starts at approval; nothing is backfilled"
        env.world.http_status["https://shop.example.com/health"] = 503
        def live_runs() -> list[str]:
            return [r.run_id for r in env.store.runs("site-watch") if r.mode.value == "live"]

        # Whoever fires a slot (this test or the worker), it must run exactly once.
        now["t"] = t0 + 9 * 60 + 30
        env.service.fire_schedules()
        [run_id] = live_runs()
        assert env.wait_for(run_id) is RunState.VERIFIED
        [alert] = env.world.messages[SLACK]
        assert "https://shop.example.com/health is not up: it answered HTTP 503" in alert["text"]
        now["t"] = t0 + 9 * 60 + 50
        env.service.fire_schedules()
        assert live_runs() == [run_id], "one run per slot"

        env.world.http_status["https://shop.example.com/health"] = 200
        now["t"] = t0 + 19 * 60 + 30
        env.service.fire_schedules()
        healthy = live_runs()[0]
        assert len(live_runs()) == 2 and healthy != run_id
        assert env.wait_for(healthy) is RunState.UNVERIFIED
        assert env.store.run(healthy).detail == "no external effect was needed"
        assert len(env.world.messages[SLACK]) == 1
    finally:
        env.service.stop()
        env.conn.close()


def test_a_new_version_shows_what_changed_before_approval(env: Env) -> None:
    env.slash(f"approve bug-reports {qualified(env)}")
    changed = env.draft(reply_when_closed=True)
    changed["budgets"]["max_runs_per_day"] = 50
    v2 = env.tool("capalis_recipe", action="compile", recipe_id="bug-reports", draft=changed)
    assert v2["version"] == 2
    changes = v2["changes_since_previous"]
    assert "added step ack (external send via capalis:discord.message.post on discord.channel:" in changes[0]
    assert any(c.startswith("can now write to discord.channel:") for c in changes)
    assert any("limits changed" in c for c in changes)
    show = env.slash("show bug-reports")
    assert "Changes since v1:" in show and "  - added step reply_fixed" in show
    asked: list[str] = []
    surfaces.command(lambda: env.service, ask=lambda q, c: asked.append(q))("approve bug-reports", platform="cli")
    env.tool("capalis_recipe", action="qualify", recipe_id="bug-reports", sample_payload=SAMPLE)
    surfaces.command(lambda: env.service, ask=lambda q, c: asked.append(q))("approve bug-reports", platform="cli")
    assert "Changed since v1:\n• added step ack" in asked[-1]


def test_recipe_export_import_carries_no_approval_and_detects_tampering(env: Env, tmp_path: Path) -> None:
    env.slash(f"approve bug-reports {qualified(env)}")
    bundle = env.tool("capalis_recipe", action="export", recipe_id="bug-reports")
    assert bundle["format"] == "capalis-recipe/1"
    assert set(bundle) == {"format", "recipe_id", "version", "digest", "intent", "recipe", "draft", "names", "note"}
    dumped = json.dumps(bundle)
    assert '"grant_id"' not in dumped and '"issued_at"' not in dumped, "no approval record is exported"

    other = Env(tmp_path / "other.db")
    try:
        imported = other.tool("capalis_recipe", action="import", recipe_id="team-bugs", bundle=bundle)
        assert imported["ok"] and imported["state"] == "compiled", imported
        assert imported["imported_from"]["digest"] == bundle["digest"]
        assert other.store.active_grant(other.store.latest("team-bugs")[0].digest) is None  # type: ignore[index]
        assert other.message("1569000000000000950") is None, "an imported Recipe does nothing until qualified"

        tampered = json.loads(json.dumps(bundle))
        tampered["recipe"]["steps"][1]["inputs"]["repo"] = {"const": "attacker/repo"}
        assert "edited" in other.tool("capalis_recipe", action="import", recipe_id="evil", bundle=tampered)["error"]
    finally:
        other.service.stop()
        other.conn.close()


def test_evidence_bundle_is_written_for_reviewers(env: Env, tmp_path: Path) -> None:
    env.service.export_dir = lambda: tmp_path / "exports"
    env.slash(f"approve bug-reports {qualified(env)}")
    sub = env.message("1569000000000000960")
    assert sub is not None and sub.run_id is not None and env.wait_for(sub.run_id) is RunState.VERIFIED
    out = env.slash(f"export {sub.run_id[:8]}")
    assert "Evidence written to:" in out
    data = json.loads((tmp_path / "exports" / f"evidence-{sub.run_id[:8]}.json").read_text())
    assert data["run"]["state"] == "verified" and data["approval"]["actor"] == "cli"
    assert [c["status"] for c in data["run"]["independent_checks"]] == ["met", "met"]
    assert "VERIFIED" in (tmp_path / "exports" / f"evidence-{sub.run_id[:8]}.md").read_text()


READ_FILE_CONTRACT = {
    "tool": "read_file", "summary": "Read a text file from the workspace", "operation": "file.read",
    "input_schema": {"type": "object", "additionalProperties": False, "required": ["path"],
                     "properties": {"path": {"type": "string"}}},
    "output_schema": {"type": "object", "required": ["content"],
                      "properties": {"content": {"type": "string"}, "total_lines": {"type": "integer"}}},
    "sample_inputs": {"path": "CHANGELOG.md"},
}


def _manual_digest_recipe(env: Env, recipe_id: str = "changelog-digest") -> dict[str, Any]:
    draft = {
        "intent": "Post the changelog's first line to engineering",
        "trigger": {"kind": "manual"},
        "steps": [
            {"id": "read", "capability": "hermes:read_file", "inputs": {"path": {"const": "CHANGELOG.md"}}},
            {"id": "post", "capability": "capalis:slack.message.post",
             "inputs": {"channel": {"const": SLACK}, "text": {"template": "Changelog: {steps.read.content}"}},
             "verify": [{"id": "post_readback", "probe": "capalis:slack.message.get",
                         "inputs": {"channel": {"const": SLACK}, "ts": {"from": "steps.post.ts"}},
                         "expect": [{"path": "text", "op": "contains", "value": {"const": "Changelog:"}}],
                         "level": "V1"}]},
        ],
        "budgets": {"max_writes_per_run": 1, "max_runs_per_day": 10},
    }
    return dict(env.tool("capalis_recipe", action="compile", recipe_id=recipe_id, draft=draft))


def test_another_hermes_tool_runs_only_after_a_passing_test_and_owner_approval(env: Env) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def dispatch(tool: str, args: dict[str, Any]) -> str:
        calls.append((tool, args))
        return json.dumps({"content": "1|# Changelog v0.1.0", "total_lines": 1})

    env.service.dispatch = dispatch
    writer = dict(READ_FILE_CONTRACT, tool="write_file", effect="external_write")
    refused = env.tool("capalis_contract", action="propose", contract=writer)
    assert "only tools that read" in " ".join(refused["problems"])

    proposed = env.tool("capalis_contract", action="propose", contract=READ_FILE_CONTRACT)
    assert proposed["ok"] and proposed["state"] == "proposed" and calls == []
    assert "E_UNKNOWN_CAPABILITY" in {p["code"] for p in _manual_digest_recipe(env)["problems"]}, \
        "a proposed contract is not runnable"
    assert "only a passing test" in env.slash("contract approve hermes:read_file")
    assert "not an approver" in env.slash("contract test hermes:read_file", STRANGER)

    tested = json.loads(env.slash("contract test hermes:read_file"))
    assert tested["passed"] and calls == [("read_file", {"path": "CHANGELOG.md"})]
    assert "can now be used in Recipes" in env.slash("contract approve hermes:read_file")

    compiled = _manual_digest_recipe(env)
    assert compiled["ok"], compiled
    env.tool("capalis_recipe", action="qualify", recipe_id="changelog-digest", sample_payload={})
    env.slash(f"approve changelog-digest {compiled['short_digest']}")
    started = env.tool("capalis_run", action="start", recipe_id="changelog-digest", payload={})
    assert started["run"]["state"] == "verified", started
    assert "Changelog: 1|# Changelog v0.1.0" in env.world.messages[SLACK][-1]["text"]

    assert "quarantined" in env.slash("contract revoke hermes:read_file")
    assert env.store.latest("changelog-digest")[1].value == "quarantined"  # type: ignore[index]


def test_a_changed_contract_must_be_tested_and_approved_again(env: Env) -> None:
    env.service.dispatch = lambda tool, args: json.dumps({"content": "x"})
    env.tool("capalis_contract", action="propose", contract=READ_FILE_CONTRACT)
    env.slash("contract test hermes:read_file")
    env.slash("contract approve hermes:read_file")
    assert "hermes:read_file" in env.service.adapters
    env.tool("capalis_contract", action="propose", contract=dict(READ_FILE_CONTRACT, summary="Read any file at all"))
    assert env.service.contracts()[0]["state"] == "proposed"
    env.service._load_contracts()
    assert "hermes:read_file" not in env.service.adapters


def test_a_failing_contract_test_cannot_be_approved(env: Env) -> None:
    env.service.dispatch = lambda tool, args: json.dumps({"error": "File not found"})
    env.tool("capalis_contract", action="propose", contract=READ_FILE_CONTRACT)
    result = json.loads(env.slash("contract test hermes:read_file"))
    assert not result["passed"] and "File not found" in result["problems"][0]
    assert "only a passing test" in env.slash("contract approve hermes:read_file")



def test_owner_gets_approval_requests_on_telegram_and_approves_with_one_command(tmp_path: Path) -> None:
    from fakes import FakeDirectory

    env = Env(tmp_path / "phone.db")
    env.service.directory = FakeDirectory(REPO, SLACK, DISCORD, REPORTER)
    env.service.alert_channel = lambda: "telegram:1145236794"
    try:
        assert env.service.send_alerts() == 0
        qualified(env)
        assert env.service.send_alerts() == 1
        [request] = env.world.telegram["1145236794"]
        assert request.startswith("🔔 Ready for your approval: bug-reports v1\n\nWhen @ada posts in #bugs")
        assert "• Open an issue in acme-labs/storefront (only if needed)\n• Post in #engineering\n" in request
        assert request.endswith("To approve it from here:\n1. Send /capalis approve\n2. Tap 1 (Approve)")

        assert env.store.claim_control("alerts_lease", ttl_s=300), "e.g. the gateway, sharing this database"
        env.store.append_event("recipe.state", {"digest": env.store.recipes()[0][2], "to": "quarantined",
                                                "reason": "test"})
        assert env.service.send_alerts() == 0, "one process sends while another holds the lease"
        env.store.release_control("alerts_lease")
        assert env.service.send_alerts() == 1 and len(env.world.telegram["1145236794"]) == 2

        env.service.approvers = lambda: ["telegram:1145236794"]
        picked: list[str] = []
        command = surfaces.command(lambda: env.service, ask=lambda q, c: picked.append(q) or "approve")
        source = type("Src", (), {"user_id": "1145236794"})()
        done = command("approve", platform="telegram", source=source)
        assert done.startswith("✅ Approved bug-reports v1 for 30 days.\n\nWhen @ada posts in #bugs, it now runs")
        assert "It can only act in: #engineering, acme-labs/storefront." in done and "sha256" not in done
        assert picked[0].startswith("Approve bug-reports v1?\n\n")
        assert command("approve", platform="telegram", source=source) == "Nothing is waiting for your approval."
    finally:
        env.service.stop()
        env.conn.close()
