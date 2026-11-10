"""Run another Hermes tool through the documented ``ctx.dispatch_tool``, as a read step or an independent check.

Hermes's agent-loop hooks don't wrap plugin dispatch (D-016); the tool's own approvals still apply, and
Capalis's runner remains the policy boundary. Only owner-approved, read-only contracts get here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..core.contracts import ContractSpec
from ..core.model import Availability, CapabilityDescriptor, Trust
from ..runtime.ports import Invocation, InvokeResult, Outcome

Dispatch = Callable[[str, dict[str, Any]], Any]


def parse_reply(spec: ContractSpec, raw: Any) -> InvokeResult:
    text = raw if isinstance(raw, str) else json.dumps(raw, default=str)
    if spec.output_format == "text":
        return InvokeResult.ok({"text": text})
    try:
        data = json.loads(text)
    except ValueError:
        return InvokeResult(Outcome.REJECTED, error="the tool did not return JSON")
    if isinstance(data, dict) and set(data) <= {"error", "success"} and data.get("error"):
        return InvokeResult(Outcome.REJECTED, error=f"{spec.tool}: {data['error']}")
    if not isinstance(data, dict):
        return InvokeResult(Outcome.REJECTED, error="the tool returned JSON that is not an object")
    return InvokeResult.ok(data)


@dataclass
class HermesToolAdapter:
    spec: ContractSpec
    dispatch: Dispatch
    available: Callable[[], bool] = lambda: True

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self.spec.descriptor(Trust.REVIEWED,
                                    Availability.AVAILABLE if self.available() else Availability.NOT_INSTALLED)

    def invoke(self, call: Invocation) -> InvokeResult:
        try:
            return parse_reply(self.spec, self.dispatch(self.spec.tool, dict(call.inputs)))
        except Exception as exc:  # a read never leaves anything uncertain behind, so it is safe to retry
            return InvokeResult(Outcome.RETRYABLE, error=f"{type(exc).__name__}: {exc}")
