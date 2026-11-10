"""Slack through the Web API (bot token) and, for comparison, an incoming webhook."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..core.model import CapabilityDescriptor
from ..runtime.ports import Invocation, InvokeResult, Outcome
from .catalog import BY_ID, SLACK_BOT_TOKEN, SLACK_WEBHOOK_URL
from .http import Response, Transport, TransportError, classify, failure, retry_after

API = "https://slack.com/api/"
EVENT_TYPE = "capalis_effect"
MAX_SCAN_PAGES = 10
_RETRYABLE = frozenset({"ratelimited", "rate_limited"})
# Errors Slack returns when it may already have processed the request.
_AMBIGUOUS = frozenset({"internal_error", "fatal_error", "request_timeout", "service_unavailable"})


@dataclass
class SlackAdapter:
    capability_id: str
    transport: Transport
    secret: Callable[[str], str | None]

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return BY_ID[self.capability_id]

    def invoke(self, call: Invocation) -> InvokeResult:
        write = self.capability_id == "capalis:slack.message.post"
        name = SLACK_WEBHOOK_URL if self.capability_id == "capalis:slack.webhook.post" else SLACK_BOT_TOKEN
        credential = self.secret(name)
        if not credential:
            return InvokeResult(Outcome.REJECTED, error=f"{name} is not configured")
        operation = {
            "capalis:slack.message.post": self._post, "capalis:slack.message.find_by_key": self._find,
            "capalis:slack.message.get": self._get, "capalis:slack.webhook.post": self._webhook,
        }[self.capability_id]
        try:
            return operation(call, credential)
        except TransportError as exc:
            return failure(exc, write=write or self.capability_id.endswith("webhook.post"))

    def _api(self, method: str, token: str, timeout: float, *, body: Mapping[str, Any] | None = None,
             query: Mapping[str, Any] | None = None) -> tuple[Response, dict[str, Any]]:
        headers = {"Authorization": f"Bearer {token}"}
        url = API + method
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            data = json.dumps(body).encode("utf-8")
            response = self.transport.send("POST", url, headers, data, timeout)
        else:
            response = self.transport.send("GET", url + "?" + urllib.parse.urlencode(query or {}), headers, None,
                                           timeout)
        try:
            parsed = response.json() or {}
        except ValueError:
            parsed = {}
        return response, parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _failure(response: Response, data: Mapping[str, Any], *, write: bool) -> InvokeResult | None:
        outcome = classify(response, write=write)
        if outcome is not None:
            return InvokeResult(outcome, error=f"Slack HTTP {response.status}", retry_after_s=retry_after(response))
        if data.get("ok") is True:
            return None
        error = str(data.get("error") or "unreadable reply")
        if error in _RETRYABLE:
            return InvokeResult(Outcome.RETRYABLE, error=f"Slack: {error}", retry_after_s=retry_after(response))
        if error in _AMBIGUOUS or (write and error == "unreadable reply"):
            return InvokeResult(Outcome.AMBIGUOUS if write else Outcome.RETRYABLE, error=f"Slack: {error}")
        return InvokeResult(Outcome.REJECTED, error=f"Slack: {error}")

    def _post(self, call: Invocation, token: str) -> InvokeResult:
        body: dict[str, Any] = {"channel": call.inputs["channel"], "text": call.inputs["text"],
                                "unfurl_links": False, "unfurl_media": False}
        if call.inputs.get("thread_ts"):
            body["thread_ts"] = call.inputs["thread_ts"]
        if call.idempotency_key:
            body["metadata"] = {"event_type": EVENT_TYPE, "event_payload": {"key": call.idempotency_key}}
        response, data = self._api("chat.postMessage", token, call.timeout_s, body=body)
        problem = self._failure(response, data, write=True)
        if problem is not None:
            return problem
        if not isinstance(data.get("ts"), str):
            return InvokeResult(Outcome.AMBIGUOUS, error="Slack accepted the message but returned no timestamp")
        return InvokeResult.ok({"ts": data["ts"], "channel": str(data.get("channel") or call.inputs["channel"])})

    def _find(self, call: Invocation, token: str) -> InvokeResult:
        channel, key = call.inputs["channel"], call.inputs["idempotency_key"]
        query: dict[str, Any] = {"channel": channel, "limit": 200, "include_all_metadata": "true"}
        # Thread replies are not in the channel history; they are read from the thread itself.
        method = "conversations.history"
        if call.inputs.get("thread_ts"):
            method, query["ts"] = "conversations.replies", call.inputs["thread_ts"]
        if isinstance(call.inputs.get("since"), int):
            query["oldest"] = str(max(0, call.inputs["since"] - 300))
        for _ in range(MAX_SCAN_PAGES):
            response, data = self._api(method, token, call.timeout_s, query=query)
            problem = self._failure(response, data, write=False)
            if problem is not None:
                return problem
            for message in data.get("messages", []):
                metadata = message.get("metadata") or {}
                if metadata.get("event_type") == EVENT_TYPE and (metadata.get("event_payload") or {}).get("key") == key:
                    return InvokeResult.ok({"found": True, "resource": {"ts": message["ts"], "channel": channel}})
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not data.get("has_more") or not cursor:
                return InvokeResult.ok({"found": False})
            query["cursor"] = cursor
        return InvokeResult(Outcome.RETRYABLE, error="too many recent messages to rule out an earlier attempt")

    def _get(self, call: Invocation, token: str) -> InvokeResult:
        ts = call.inputs["ts"]
        query: dict[str, Any] = {"channel": call.inputs["channel"], "latest": ts, "oldest": ts, "inclusive": "true",
                                 "limit": 1}
        method = "conversations.history"
        if call.inputs.get("thread_ts"):
            method, query = "conversations.replies", {"channel": call.inputs["channel"], "ts": call.inputs["thread_ts"],
                                                      "oldest": ts, "inclusive": "true", "limit": 200}
        response, data = self._api(method, token, call.timeout_s, query=query)
        problem = self._failure(response, data, write=False)
        if problem is not None:
            return problem
        for message in data.get("messages", []):
            if message.get("ts") == ts:
                return InvokeResult.ok({"text": str(message.get("text") or ""), "ts": ts})
        return InvokeResult(Outcome.REJECTED, error="message not found")

    def _webhook(self, call: Invocation, url: str) -> InvokeResult:
        if not url.startswith("https://hooks.slack.com/"):
            return InvokeResult(Outcome.REJECTED, error=f"{SLACK_WEBHOOK_URL} is not a Slack incoming webhook URL")
        body = json.dumps({"text": call.inputs["text"]}).encode("utf-8")
        response = self.transport.send("POST", url, {"Content-Type": "application/json"}, body, call.timeout_s)
        outcome = classify(response, write=True)
        if outcome is not None:
            return InvokeResult(outcome, error=f"Slack webhook HTTP {response.status}")
        return InvokeResult.ok({"ok": True})
