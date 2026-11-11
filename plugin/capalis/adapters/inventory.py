"""The executable capability set: Capalis's reviewed adapters, with availability from configured secrets."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..core.model import CapabilityDescriptor
from ..runtime.ports import Invocation, InvokeResult
from .catalog import CATALOG, with_availability
from .discord import DiscordAdapter
from .github import GitHubAdapter
from .http import Transport, UrllibTransport
from .slack import SlackAdapter
from .telegram import TelegramAdapter
from .web import HttpGetAdapter

SecretReader = Callable[[str], str | None]


def hermes_secret(name: str) -> str | None:
    # Catalog rule 11: credentials come only through Hermes's profile-scoped secret route.
    from agent.secret_scope import get_secret

    value = get_secret(name)
    return value or None


@dataclass
class _Available:
    """Wraps an adapter so its descriptor reports whether its credential is configured right now."""

    inner: Any
    secret: SecretReader

    @property
    def descriptor(self) -> CapabilityDescriptor:
        cap = self.inner.descriptor
        return with_availability(cap, cap.auth_requirement is None or bool(self.secret(cap.auth_requirement)))

    def invoke(self, call: Invocation) -> InvokeResult:
        result: InvokeResult = self.inner.invoke(call)
        return result


def build(secret: SecretReader = hermes_secret, transport: Transport | None = None,
          web_transport: Transport | None = None) -> dict[str, Any]:
    transport = transport or UrllibTransport()
    # Web reads go to model-chosen origins, so the real client only connects to checked public addresses.
    web_transport = web_transport or (UrllibTransport(public_only=True) if isinstance(transport, UrllibTransport)
                                      else transport)
    adapters: dict[str, Any] = {}
    for cap in CATALOG:
        if cap.provider == "github":
            inner: Any = GitHubAdapter(cap.id, transport, secret)
        elif cap.provider == "slack":
            inner = SlackAdapter(cap.id, transport, secret)
        elif cap.provider == "discord":
            inner = DiscordAdapter(cap.id, transport, secret)
        elif cap.provider == "telegram":
            inner = TelegramAdapter(transport, secret)
        else:
            inner = HttpGetAdapter(web_transport)
        adapters[cap.id] = _Available(inner, secret)
    return adapters


def descriptors(adapters: Mapping[str, Any]) -> dict[str, CapabilityDescriptor]:
    return {cid: adapter.descriptor for cid, adapter in adapters.items()}

