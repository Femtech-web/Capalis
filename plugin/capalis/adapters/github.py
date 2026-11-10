"""GitHub Issues through the REST API, with a fine-grained token scoped to the sandbox repository."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..core.model import CapabilityDescriptor
from ..runtime.ports import Invocation, InvokeResult, Outcome
from .catalog import BY_ID, GITHUB_TOKEN
from .http import Response, Transport, TransportError, classify, failure, retry_after

API = "https://api.github.com"
MARKER = "<!-- capalis:{key} -->"
_MARKER_RE = re.compile(r"<!-- capalis:(cap-[0-9a-f]{32}) -->")
# Reconciliation scans issues updated since the attempt; past this many pages it reports an error
# rather than "not found", since "not found" would license another write.
MAX_SCAN_PAGES = 10
_QUOTE_LIKE = re.compile(r"[\"\\\r\n\t]")


def _issue(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"number": raw["number"], "title": raw.get("title") or "", "state": raw.get("state") or "",
            "state_reason": raw.get("state_reason") or "", "html_url": raw.get("html_url") or "",
            "body": raw.get("body") or "",
            "labels": [label["name"] if isinstance(label, Mapping) else str(label) for label in raw.get("labels", [])]}


def _iso(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


@dataclass
class GitHubAdapter:
    capability_id: str
    transport: Transport
    secret: Callable[[str], str | None]

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return BY_ID[self.capability_id]

    def invoke(self, call: Invocation) -> InvokeResult:
        token = self.secret(GITHUB_TOKEN)
        if not token:
            return InvokeResult(Outcome.REJECTED, error=f"{GITHUB_TOKEN} is not configured")
        operation = {
            "capalis:github.issue.search": self._search, "capalis:github.issue.create": self._create,
            "capalis:github.issue.find_by_key": self._find, "capalis:github.issue.get": self._get,
        }[self.capability_id]
        try:
            return operation(call, token)
        except TransportError as exc:
            return failure(exc, write=self.capability_id.endswith(".create"))

    def _request(self, method: str, path: str, token: str, timeout: float, body: Any = None) -> Response:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        return self.transport.send(method, API + path, headers, data, timeout)

    @staticmethod
    def _error(response: Response, outcome: Outcome) -> InvokeResult:
        try:
            message = (response.json() or {}).get("message", "")
        except (ValueError, AttributeError):
            message = ""
        return InvokeResult(outcome, error=f"GitHub {response.status} {message}".strip(),
                            retry_after_s=retry_after(response))

    def _search(self, call: Invocation, token: str) -> InvokeResult:
        repo = call.inputs["repo"]
        # The phrase is quoted and stripped of quote characters, so report text cannot add qualifiers
        # such as another repo: to the search.
        phrase = _QUOTE_LIKE.sub(" ", call.inputs["query"]).strip()
        query = urllib.parse.quote(f'repo:{repo} is:issue "{phrase}"')
        response = self._request("GET", f"/search/issues?q={query}&per_page=20", token, call.timeout_s)
        outcome = classify(response, write=False)
        if outcome is not None:
            return self._error(response, outcome)
        data = response.json() or {}
        prefix = f"{API}/repos/{repo}".lower()
        items = [_issue(item) for item in data.get("items", [])
                 if str(item.get("repository_url", "")).lower() == prefix and "pull_request" not in item]
        return InvokeResult.ok({"total_count": len(items), "items": items})

    def _create(self, call: Invocation, token: str) -> InvokeResult:
        body = call.inputs["body"]
        if call.idempotency_key:
            body = f"{body}\n\n{MARKER.format(key=call.idempotency_key)}"
        payload: dict[str, Any] = {"title": call.inputs["title"], "body": body}
        if call.inputs.get("labels"):
            payload["labels"] = list(call.inputs["labels"])
        response = self._request("POST", f"/repos/{call.inputs['repo']}/issues", token, call.timeout_s, payload)
        outcome = classify(response, write=True)
        if outcome is not None:
            return self._error(response, outcome)
        try:
            return InvokeResult.ok(_issue(response.json()))
        except (ValueError, KeyError, TypeError) as exc:
            return InvokeResult(Outcome.AMBIGUOUS,
                                error=f"GitHub accepted the request but the reply was unreadable: {exc}")

    def _find(self, call: Invocation, token: str) -> InvokeResult:
        repo, key = call.inputs["repo"], call.inputs["idempotency_key"]
        since = call.inputs.get("since")
        # The issues list is strongly consistent, unlike search, so a fresh issue is always seen here.
        query = "state=all&sort=created&direction=desc&per_page=100"
        if isinstance(since, int):
            query += "&since=" + urllib.parse.quote(_iso(max(0, since - 300)))
        for page in range(1, MAX_SCAN_PAGES + 1):
            response = self._request("GET", f"/repos/{repo}/issues?{query}&page={page}", token, call.timeout_s)
            outcome = classify(response, write=False)
            if outcome is not None:
                return self._error(response, outcome)
            items = response.json() or []
            for item in items:
                if "pull_request" in item:
                    continue
                if any(match == key for match in _MARKER_RE.findall(item.get("body") or "")):
                    return InvokeResult.ok({"found": True, "resource": _issue(item)})
            if len(items) < 100:
                return InvokeResult.ok({"found": False})
        return InvokeResult(Outcome.RETRYABLE, error="too many recent issues to rule out an earlier attempt")

    def _get(self, call: Invocation, token: str) -> InvokeResult:
        response = self._request("GET", f"/repos/{call.inputs['repo']}/issues/{int(call.inputs['number'])}", token,
                                 call.timeout_s)
        outcome = classify(response, write=False)
        if outcome is not None:
            return self._error(response, outcome)
        raw = response.json() or {}
        if "pull_request" in raw:
            return InvokeResult(Outcome.REJECTED, error="that number is a pull request, not an issue")
        return InvokeResult.ok(_issue(raw))
