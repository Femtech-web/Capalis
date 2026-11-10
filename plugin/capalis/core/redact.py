"""Redaction applied before anything is persisted, logged, exported or sent to a model (I-06)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

REDACTED = "[redacted]"
MIN_SECRET_LENGTH = 8

_TOKEN_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"xapp-[A-Za-z0-9-]{10,}"),
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"),
    re.compile(r"\b[0-9]{6,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\b[MN][A-Za-z0-9_-]{23,27}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,40}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


class Redactor:
    """Masks known secret values exactly, plus common credential formats by pattern."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        values = {s for s in secrets if isinstance(s, str) and len(s) >= MIN_SECRET_LENGTH}
        # Longest first, so a secret that contains another is masked whole.
        self._secrets = sorted(values, key=len, reverse=True)

    def text(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, REDACTED)
        for pattern in _TOKEN_PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {self.text(str(k)): self.value(v) for k, v in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            return [self.value(v) for v in value]
        return value
