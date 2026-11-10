"""Discord channel messages through the REST API, as the Capalis bot.

Discord messages cannot carry hidden metadata, so an earlier attempt is identified by what only that
step would have written: a message by this bot, replying to the same message, with the same text. The
request also sends a key-derived nonce with ``enforce_nonce``, which makes Discord itself drop quick
duplicates.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..core.model import CapabilityDescriptor
from ..runtime.ports import Invocation, InvokeResult, Outcome
from .catalog import BY_ID, DISCORD_BOT_TOKEN
from .http import Response, Transport, TransportError, classify, failure, retry_after

API = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000
MAX_SCAN_PAGES = 10


def snowflake_at(epoch_s: int) -> int:
    return max(0, (epoch_s * 1000 - DISCORD_EPOCH_MS)) << 22


def nonce_for(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:25]


def _message(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": str(raw["id"]), "channel": str(raw.get("channel_id") or ""), "text": str(raw.get("content") or "")}


@dataclass
class DiscordAdapter:
    capability_id: str
    transport: Transport
    secret: Callable[[str], str | None]
    _bot_id: str | None = field(default=None, init=False)

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return BY_ID[self.capability_id]

    def invoke(self, call: Invocation) -> InvokeResult:
        token = self.secret(DISCORD_BOT_TOKEN)
        if not token:
            return InvokeResult(Outcome.REJECTED, error=f"{DISCORD_BOT_TOKEN} is not configured")
        operation = {"capalis:discord.message.post": self._post, "capalis:discord.message.find_by_key": self._find,
                     "capalis:discord.message.get": self._get}[self.capability_id]
        try:
            return operation(call, token)
        except TransportError as exc:
            return failure(exc, write=self.capability_id.endswith(".post"))

    def _request(self, method: str, path: str, token: str, timeout: float, body: Any = None) -> Response:
        headers = {"Authorization": f"Bot {token}"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        return self.transport.send(method, API + path, headers, data, timeout)

    @staticmethod
    def _error(response: Response, outcome: Outcome) -> InvokeResult:
        try:
            data = response.json() or {}
            detail = f"{data.get('code', '')} {data.get('message', '')}".strip()
        except (ValueError, AttributeError):
            detail = ""
        return InvokeResult(outcome, error=f"Discord {response.status} {detail}".strip(),
                            retry_after_s=retry_after(response))

    def _post(self, call: Invocation, token: str) -> InvokeResult:
        body: dict[str, Any] = {"content": call.inputs["text"], "allowed_mentions": {"parse": []}}
        if call.inputs.get("reply_to"):
            body["message_reference"] = {"message_id": call.inputs["reply_to"], "fail_if_not_exists": False}
        if call.idempotency_key:
            body["nonce"] = nonce_for(call.idempotency_key)
            body["enforce_nonce"] = True
        response = self._request("POST", f"/channels/{call.inputs['channel']}/messages", token, call.timeout_s, body)
        outcome = classify(response, write=True)
        if outcome is not None:
            return self._error(response, outcome)
        try:
            return InvokeResult.ok(_message(response.json()))
        except (ValueError, KeyError, TypeError) as exc:
            return InvokeResult(Outcome.AMBIGUOUS, error=f"Discord accepted the message but the reply was unreadable: "
                                                         f"{exc}")

    def _whoami(self, token: str, timeout: float) -> str | None:
        if self._bot_id is None:
            response = self._request("GET", "/users/@me", token, timeout)
            if classify(response, write=False) is None:
                self._bot_id = str((response.json() or {}).get("id") or "") or None
        return self._bot_id

    def _find(self, call: Invocation, token: str) -> InvokeResult:
        channel, reply_to, text = call.inputs["channel"], call.inputs.get("reply_to"), call.inputs.get("text")
        if not text:
            return InvokeResult(Outcome.REJECTED, error="the lookup needs the text the step would have posted")
        bot_id = self._whoami(token, call.timeout_s)
        if bot_id is None:
            return InvokeResult(Outcome.RETRYABLE, error="could not identify the Capalis bot")
        after = snowflake_at(int(call.inputs["since"]) - 300) if isinstance(call.inputs.get("since"), int) else 0
        for _ in range(MAX_SCAN_PAGES):
            response = self._request("GET", f"/channels/{channel}/messages?limit=100&after={after}", token,
                                     call.timeout_s)
            outcome = classify(response, write=False)
            if outcome is not None:
                return self._error(response, outcome)
            page = response.json() or []
            for raw in page:
                reference = (raw.get("message_reference") or {}).get("message_id")
                if (str((raw.get("author") or {}).get("id")) == bot_id and raw.get("content") == text
                        and (reply_to is None or str(reference) == str(reply_to))):
                    return InvokeResult.ok({"found": True, "resource": _message(raw)})
            if len(page) < 100:
                return InvokeResult.ok({"found": False})
            after = max(int(raw["id"]) for raw in page)
        return InvokeResult(Outcome.RETRYABLE, error="too many recent messages to rule out an earlier attempt")

    def _get(self, call: Invocation, token: str) -> InvokeResult:
        response = self._request("GET", f"/channels/{call.inputs['channel']}/messages/{call.inputs['id']}", token,
                                 call.timeout_s)
        outcome = classify(response, write=False)
        if outcome is not None:
            return self._error(response, outcome)
        return InvokeResult.ok(_message(response.json() or {}))
