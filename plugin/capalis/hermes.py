"""Hermes glue: the service on the documented plugin storage, credentials via get_secret, gateway intake."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from typing import Any

from .adapters import inventory
from .adapters.catalog import DISCORD_BOT_TOKEN, GITHUB_TOKEN, SLACK_BOT_TOKEN, SLACK_WEBHOOK_URL
from .adapters.directory import Directory
from .adapters.http import UrllibTransport
from .core.redact import Redactor
from .service import Capalis
from .storage.db import Store

log = logging.getLogger("capalis")
PLUGIN = "capalis"
SKIP = {"action": "skip", "reason": "capalis: message belongs to a Recipe trigger"}


class ServiceHolder:
    """Builds the service on first use, so loading the plugin never opens files or reads secrets."""

    def __init__(self, ctx: Any, *, factory: Callable[[], Capalis] | None = None) -> None:
        self.ctx = ctx
        self._factory = factory or self._build
        self._service: Capalis | None = None
        self._lock = threading.Lock()

    def get(self) -> Capalis:
        with self._lock:
            if self._service is None:
                self._service = self._factory()
            return self._service

    def _approvers(self) -> Sequence[str]:
        try:
            settings = self.ctx.get_config() or {}
        except Exception:
            return ()
        value = settings.get("approvers") if isinstance(settings, dict) else None
        return [str(v) for v in value] if isinstance(value, list) else ()

    def _build(self) -> Capalis:
        from plugins.plugin_storage import plugin_db

        secrets = [v for name in (GITHUB_TOKEN, SLACK_BOT_TOKEN, SLACK_WEBHOOK_URL, DISCORD_BOT_TOKEN)
                   if (v := inventory.hermes_secret(name))]
        store = Store(plugin_db(PLUGIN, "capalis.db"), redactor=Redactor(secrets))
        transport = UrllibTransport()
        service = Capalis(store, inventory.build(transport=transport), approvers=self._approvers,
                          directory=Directory(transport, inventory.hermes_secret))
        service.ensure_worker()
        return service

    def on_gateway_message(self, event: Any = None, **_: Any) -> dict[str, str] | None:
        """pre_gateway_dispatch: messages in a channel a Recipe listens to are Capalis's, not the agent's."""
        try:
            source = getattr(event, "source", None)
            if event is None or source is None or getattr(event, "internal", False) or getattr(source, "is_bot", False):
                return None
            platform = getattr(getattr(source, "platform", None), "value", None) or str(source.platform)
            chat_id = str(getattr(source, "chat_id", "") or "")
            sender = str(getattr(source, "user_id", "") or getattr(event, "user_id", "") or "")
            message_id = str(getattr(event, "message_id", "") or getattr(source, "message_id", "") or "")
            if not chat_id or not sender or not message_id:
                return None
            submission = self.get().on_message(
                platform=platform, chat_id=chat_id, sender=sender, message_id=message_id,
                author=str(getattr(source, "user_name", "") or sender), text=str(getattr(event, "text", "") or ""))
        except Exception:
            # Never take down the gateway's dispatch; an unhandled message just reaches the agent.
            log.exception("capalis: gateway intake failed")
            return None
        return None if submission is None else SKIP
