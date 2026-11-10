"""Credential-free HTTPS GET against an origin the Recipe names as its target."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.model import CapabilityDescriptor
from ..runtime.ports import Invocation, InvokeResult, Outcome
from .catalog import BY_ID
from .http import Transport, TransportError, failure

MAX_TEXT = 16_000


@dataclass
class HttpGetAdapter:
    transport: Transport
    capability_id: str = "capalis:http.get"

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return BY_ID[self.capability_id]

    def invoke(self, call: Invocation) -> InvokeResult:
        origin, path = call.inputs["origin"], call.inputs["path"]
        if call.target is None or call.target.value != origin or not path.startswith("/") or path.startswith("//"):
            return InvokeResult(Outcome.REJECTED, error="the request does not stay on the approved origin")
        try:
            response = self.transport.send("GET", origin + path, {"Accept": "text/plain, application/json, */*"},
                                           None, call.timeout_s)
        except TransportError as exc:
            return failure(exc, write=False)
        # Any HTTP status is an observation here; a wait or check decides what it means.
        text = response.body[:MAX_TEXT].decode("utf-8", errors="replace")
        return InvokeResult.ok({"status": response.status, "body_text": text})
