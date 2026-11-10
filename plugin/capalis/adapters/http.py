"""A small HTTPS client for provider adapters, with failure classification the runner can rely on.

Stdlib only. Redirects are never followed (a redirect would carry credentials to a host nobody
approved), responses are size-capped, and every failure is classified: a request that may have been
received is *ambiguous* for writes, never a clean failure.
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..runtime.ports import InvokeResult, Outcome

MAX_BODY = 1_000_000
USER_AGENT = "capalis/0.1 (+https://github.com/NousResearch/hermes-agent)"


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None


class TransportError(Exception):
    """``sent`` is False only when the request provably never left this machine."""

    def __init__(self, message: str, *, sent: bool) -> None:
        super().__init__(message)
        self.sent = sent


class Transport(Protocol):
    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None,
             timeout: float) -> Response: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _headers(raw: Any) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in raw.items()} if raw is not None else {}


def _tls_context() -> ssl.SSLContext:
    try:
        import certifi  # present in Hermes's environment; standalone Pythons may lack system roots
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


@dataclass
class UrllibTransport:
    _opener: Any = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=_tls_context()))

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None,
             timeout: float) -> Response:
        if not url.startswith("https://"):
            raise TransportError("only https URLs are allowed", sent=False)
        request = urllib.request.Request(url, data=body, method=method,
                                         headers={"User-Agent": USER_AGENT, **headers})
        try:
            with self._opener.open(request, timeout=timeout) as raw:
                return Response(raw.status, _headers(raw.headers), raw.read(MAX_BODY + 1)[:MAX_BODY])
        except urllib.error.HTTPError as exc:
            return Response(exc.code, _headers(exc.headers), exc.read(MAX_BODY))
        except urllib.error.URLError as exc:
            reason = exc.reason
            # DNS failure or a refused connection: nothing was transmitted.
            never_sent = isinstance(reason, (socket.gaierror, ConnectionRefusedError, ssl.SSLCertVerificationError))
            raise TransportError(f"{type(reason).__name__}: {reason}", sent=not never_sent) from exc
        except (TimeoutError, ConnectionError, ssl.SSLError, OSError) as exc:
            raise TransportError(f"{type(exc).__name__}: {exc}", sent=True) from exc


def classify(response: Response, *, write: bool) -> Outcome | None:
    """None for success; otherwise how the runner must treat the failure."""
    if 200 <= response.status < 300:
        return None
    if response.status == 429 or (response.status == 403 and response.headers.get("x-ratelimit-remaining") == "0"):
        return Outcome.RETRYABLE
    if response.status in (408, 409) or response.status >= 500:
        return Outcome.AMBIGUOUS if write else Outcome.RETRYABLE
    return Outcome.REJECTED


def retry_after(response: Response) -> int | None:
    value = response.headers.get("retry-after")
    try:
        return max(1, min(3600, int(float(value)))) if value else None
    except ValueError:
        return None


def failure(exc: TransportError, *, write: bool) -> InvokeResult:
    if not exc.sent:
        return InvokeResult(Outcome.RETRYABLE, error=f"not sent: {exc}")
    return InvokeResult(Outcome.AMBIGUOUS if write else Outcome.RETRYABLE, error=f"no answer: {exc}")
