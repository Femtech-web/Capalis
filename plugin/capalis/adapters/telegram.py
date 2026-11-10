"""Telegram through the Bot API: the owner's phone, for alerts and approval requests.

The Bot API has no call that reads a sent message back, so a Telegram post is honestly unverifiable: the
reply to sendMessage is only the tool's own word (V0). Capalis uses Telegram for messages to the owner,
where that is enough, and a Recipe that posts to Telegram has to say so with an unverifiable reason.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..core.model import CapabilityDescriptor
from ..runtime.ports import Invocation, InvokeResult, Outcome
from .catalog import BY_ID, TELEGRAM_BOT_TOKEN
from .http import Transport, TransportError, failure

API = "https://api.telegram.org"


@dataclass
class TelegramAdapter:
    transport: Transport
    secret: Callable[[str], str | None]
    capability_id: str = "capalis:telegram.message.post"

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return BY_ID[self.capability_id]

    def invoke(self, call: Invocation) -> InvokeResult:
        token = self.secret(TELEGRAM_BOT_TOKEN)
        if not token:
            return InvokeResult(Outcome.REJECTED, error=f"{TELEGRAM_BOT_TOKEN} is not configured")
        body = json.dumps({"chat_id": call.inputs["chat"], "text": call.inputs["text"],
                           "disable_web_page_preview": True}).encode("utf-8")
        try:
            # The token is part of Telegram's URL; errors below never include the URL.
            response = self.transport.send("POST", f"{API}/bot{token}/sendMessage",
                                           {"Content-Type": "application/json"}, body, call.timeout_s)
        except TransportError as exc:
            return failure(exc, write=True)
        try:
            data: Any = response.json() or {}
        except ValueError:
            data = {}
        if response.status == 200 and data.get("ok") and isinstance(data.get("result"), dict):
            result = data["result"]
            return InvokeResult.ok({"message_id": int(result.get("message_id", 0)),
                                    "chat": str((result.get("chat") or {}).get("id", call.inputs["chat"]))})
        description = str(data.get("description") or f"HTTP {response.status}")
        if response.status == 429:
            retry = (data.get("parameters") or {}).get("retry_after")
            return InvokeResult(Outcome.RETRYABLE, error=f"Telegram: {description}",
                                retry_after_s=int(retry) if isinstance(retry, int) else None)
        if response.status >= 500 or response.status == 200:
            return InvokeResult(Outcome.AMBIGUOUS, error=f"Telegram: {description}")
        return InvokeResult(Outcome.REJECTED, error=f"Telegram: {description}")
