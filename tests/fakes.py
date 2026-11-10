"""In-memory stand-ins for GitHub, Slack and an HTTP origin, with injectable failure modes.

They are test doubles for the runner only. Provider adapters are tested against recorded HTTP
exchanges and real accounts, never against these.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from capalis.core.model import CapabilityDescriptor
from capalis.runtime.ports import Invocation, InvokeResult, Outcome
from support import registry


class Crash(BaseException):
    """Simulates the process dying mid-call: not an Exception, so nothing in the runner catches it."""


@dataclass
class Fault:
    mode: str  # "ambiguous_after", "ambiguous_before", "retryable", "rejected", "lie", "crash_after", "raise"
    times: int = 1
    lie_output: dict[str, Any] | None = None


@dataclass
class World:
    issues: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    messages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    discord: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    http_status: dict[str, int] = field(default_factory=dict)
    calls: list[Invocation] = field(default_factory=list)
    faults: dict[str, list[Fault]] = field(default_factory=dict)
    hooks: dict[str, Callable[[Invocation], None]] = field(default_factory=dict)

    def fault(self, capability_id: str, mode: str, times: int = 1, **kw: Any) -> None:
        self.faults.setdefault(capability_id, []).append(Fault(mode, times, **kw))

    def calls_to(self, capability_id: str) -> list[Invocation]:
        return [c for c in self.calls if c.capability_id == capability_id]

    @property
    def writes(self) -> list[Invocation]:
        caps = registry()
        return [c for c in self.calls if caps[c.capability_id].effect.value != "read"]

    # -- providers ----------------------------------------------------------------------------------

    def _issue_create(self, call: Invocation) -> dict[str, Any]:
        repo = self.issues.setdefault(call.inputs["repo"], [])
        number = len(repo) + 1
        marker = f"\n<!-- capalis:{call.idempotency_key} -->" if call.idempotency_key else ""
        issue = {"number": number, "title": call.inputs["title"], "state": "open", "state_reason": "",
                 "html_url": f"https://github.com/{call.inputs['repo']}/issues/{number}",
                 "body": call.inputs["body"] + marker, "labels": list(call.inputs.get("labels", []))}
        repo.append(issue)
        return dict(issue)

    def _issue_search(self, call: Invocation) -> dict[str, Any]:
        needle = call.inputs["query"]
        items = [dict(i) for i in self.issues.get(call.inputs["repo"], []) if needle in i["body"]]
        return {"total_count": len(items), "items": items}

    def _issue_find(self, call: Invocation) -> dict[str, Any]:
        key = call.inputs["idempotency_key"]
        for issue in self.issues.get(call.inputs["repo"], []):
            if f"capalis:{key}" in issue["body"]:
                return {"found": True, "resource": dict(issue)}
        return {"found": False}

    def _issue_get(self, call: Invocation) -> InvokeResult:
        for issue in self.issues.get(call.inputs["repo"], []):
            if issue["number"] == call.inputs["number"]:
                return InvokeResult.ok(dict(issue))
        return InvokeResult(Outcome.REJECTED, error="404 Not Found")

    def _message_post(self, call: Invocation) -> dict[str, Any]:
        channel = self.messages.setdefault(call.inputs["channel"], [])
        ts = f"1800000000.{len(channel) + 1:06d}"
        channel.append({"ts": ts, "channel": call.inputs["channel"], "text": call.inputs["text"],
                        "key": call.idempotency_key})
        return {"ts": ts, "channel": call.inputs["channel"]}

    def _message_find(self, call: Invocation) -> dict[str, Any]:
        for message in self.messages.get(call.inputs["channel"], []):
            if message["key"] == call.inputs["idempotency_key"]:
                return {"found": True, "resource": {"ts": message["ts"], "channel": message["channel"]}}
        return {"found": False}

    def _message_get(self, call: Invocation) -> InvokeResult:
        for message in self.messages.get(call.inputs["channel"], []):
            if message["ts"] == call.inputs["ts"]:
                return InvokeResult.ok({"text": message["text"], "ts": message["ts"]})
        return InvokeResult(Outcome.REJECTED, error="message_not_found")

    def _webhook_post(self, call: Invocation) -> dict[str, Any]:
        self.messages.setdefault(call.inputs["channel"], []).append(
            {"ts": "", "channel": call.inputs["channel"], "text": call.inputs["text"], "key": None})
        return {"ok": True}

    def close_issue(self, repo: str, number: int, reason: str = "completed") -> None:
        issue = next(i for i in self.issues[repo] if i["number"] == number)
        issue.update(state="closed", state_reason=reason)

    def _discord_post(self, call: Invocation) -> dict[str, Any]:
        channel = self.discord.setdefault(call.inputs["channel"], [])
        message = {"id": str(1569000000000000000 + len(channel) + 1), "channel": call.inputs["channel"],
                   "text": call.inputs["text"], "reply_to": call.inputs.get("reply_to")}
        channel.append(message)
        return {"id": message["id"], "channel": message["channel"], "text": message["text"]}

    def _discord_find(self, call: Invocation) -> dict[str, Any]:
        for m in self.discord.get(call.inputs["channel"], []):
            if m["text"] == call.inputs.get("text") and m["reply_to"] == call.inputs.get("reply_to"):
                return {"found": True, "resource": {"id": m["id"], "channel": m["channel"], "text": m["text"]}}
        return {"found": False}

    def _discord_get(self, call: Invocation) -> InvokeResult:
        for m in self.discord.get(call.inputs["channel"], []):
            if m["id"] == call.inputs["id"]:
                return InvokeResult.ok({"id": m["id"], "channel": m["channel"], "text": m["text"]})
        return InvokeResult(Outcome.REJECTED, error="Unknown Message")

    def _http_get(self, call: Invocation) -> dict[str, Any]:
        url = call.inputs["origin"] + call.inputs["path"]
        return {"status": self.http_status.get(url, 404), "body_text": ""}

    def handler(self, capability_id: str) -> Callable[[Invocation], Any]:
        return {
            "capalis:github.issue.create": self._issue_create,
            "capalis:github.issue.search": self._issue_search,
            "capalis:github.issue.find_by_key": self._issue_find,
            "capalis:github.issue.get": self._issue_get,
            "capalis:slack.message.post": self._message_post,
            "capalis:slack.message.find_by_key": self._message_find,
            "capalis:slack.message.get": self._message_get,
            "capalis:slack.webhook.post": self._webhook_post,
            "capalis:http.get": self._http_get,
            "capalis:discord.message.post": self._discord_post,
            "capalis:discord.message.find_by_key": self._discord_find,
            "capalis:discord.message.get": self._discord_get,
        }[capability_id]

    def invoke(self, call: Invocation) -> InvokeResult:
        self.calls.append(call)
        if call.capability_id in self.hooks:
            self.hooks[call.capability_id](call)
        pending = self.faults.get(call.capability_id, [])
        fault = pending[0] if pending else None
        if fault is not None:
            fault.times -= 1
            if fault.times <= 0:
                pending.pop(0)
            if fault.mode == "ambiguous_before":
                return InvokeResult(Outcome.AMBIGUOUS, error="read timed out")
            if fault.mode == "retryable":
                return InvokeResult(Outcome.RETRYABLE, error="429 rate limited", retry_after_s=1)
            if fault.mode == "rejected":
                return InvokeResult(Outcome.REJECTED, error="422 validation failed")
            if fault.mode == "lie":
                return InvokeResult.ok(fault.lie_output or {})
            if fault.mode == "raise":
                raise RuntimeError("adapter bug")
        result = self.handler(call.capability_id)(call)
        if fault is not None and fault.mode == "ambiguous_after":
            return InvokeResult(Outcome.AMBIGUOUS, error="connection reset after sending")
        if fault is not None and fault.mode == "crash_after":
            raise Crash
        return result if isinstance(result, InvokeResult) else InvokeResult.ok(result)


@dataclass
class FakeAdapter:
    world: World
    _descriptor: CapabilityDescriptor

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    def invoke(self, call: Invocation) -> InvokeResult:
        return self.world.invoke(call)


def adapters(world: World) -> dict[str, FakeAdapter]:
    return {cid: FakeAdapter(world, cap) for cid, cap in registry().items()
            if cid.startswith("capalis:") and cid != "capalis:github.issue.delete"}


def drifted(adapter: FakeAdapter, **changes: Any) -> FakeAdapter:
    return FakeAdapter(adapter.world, replace(adapter.descriptor, **changes))


class FakeDirectory:
    """Names as the owner would type them, mapped to the support fixture's ids."""

    def __init__(self, repo: str, slack: str, discord: str, reporter: str) -> None:
        from capalis.adapters.directory import Resolved

        self.resolved = Resolved
        self.repo, self.slack, self.discord, self.reporter = repo, slack, discord, reporter

    def github_repo(self, name: str) -> Any:
        return self.resolved(self.repo, self.repo)

    def slack_channel(self, name: str) -> Any:
        return self.resolved(self.slack, "#engineering")

    def discord_channel(self, name: str) -> Any:
        return self.resolved(self.discord, "#bugs", "in Capalis Test")

    def discord_user(self, name: str, channel_id: str) -> Any:
        from capalis.adapters.directory import ResolveError

        if name.lstrip("@") != "ada":
            raise ResolveError(f"could not find {name} in #bugs")
        return self.resolved(self.reporter, "@ada")
