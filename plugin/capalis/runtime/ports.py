"""The boundary between the runner and anything that touches the outside world."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from ..core.model import CapabilityDescriptor, Target


class Outcome(StrEnum):
    OK = "ok"
    # Definitely not applied, and retrying will not help (bad request, permission denied).
    REJECTED = "rejected"
    # Definitely not applied, but may succeed later (rate limited, refused before the write).
    RETRYABLE = "retryable"
    # May or may not have been applied (timeout or connection loss after sending, 5xx on a write).
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class Invocation:
    capability_id: str
    target: Target | None
    inputs: Mapping[str, Any]
    idempotency_key: str | None
    timeout_s: int


@dataclass(frozen=True, slots=True)
class InvokeResult:
    outcome: Outcome
    output: Mapping[str, Any] | None = None
    error: str | None = None
    retry_after_s: int | None = None

    @classmethod
    def ok(cls, output: Mapping[str, Any]) -> InvokeResult:
        return cls(Outcome.OK, output)


class Adapter(Protocol):
    """One reviewed capability. Adapters classify their own failures; the runner never guesses."""

    @property
    def descriptor(self) -> CapabilityDescriptor: ...

    def invoke(self, call: Invocation) -> InvokeResult: ...
