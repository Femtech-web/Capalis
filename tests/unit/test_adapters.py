from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from capalis.adapters import inventory
from capalis.adapters.catalog import CATALOG, GITHUB_TOKEN, SLACK_BOT_TOKEN, SLACK_WEBHOOK_URL
from capalis.adapters.github import GitHubAdapter
from capalis.adapters.http import Response, TransportError, UrllibTransport
from capalis.adapters.slack import SlackAdapter
from capalis.adapters.web import HttpGetAdapter
from capalis.core.model import Availability, Target
from capalis.runtime.ports import Invocation, Outcome

REPO = "oluwafemi-akolade/capalis-sandbox"
KEY = "cap-" + "a" * 32
TOKEN = "ghp_" + "x" * 36
SECRETS = {GITHUB_TOKEN: TOKEN, SLACK_BOT_TOKEN: "xoxb-1-2-" + "y" * 24,
           SLACK_WEBHOOK_URL: "https://hooks.slack.com/services/T0/B0/zzzz"}


@dataclass
class Sent:
    method: str
    url: str
    headers: Mapping[str, str]
    body: Any


@dataclass
class Script:
    replies: list[Response | Exception]
    sent: list[Sent] = field(default_factory=list)

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None,
             timeout: float) -> Response:
        self.sent.append(Sent(method, url, headers, json.loads(body) if body else None))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def reply(status: int, data: Any, **headers: str) -> Response:
    return Response(status, {k.replace("_", "-"): v for k, v in headers.items()}, json.dumps(data).encode())


def issue(number: int, body: str = "text", repo: str = REPO, **extra: Any) -> dict[str, Any]:
    return {"number": number, "title": "t", "state": "open", "body": body,
            "html_url": f"https://github.com/{repo}/issues/{number}", "labels": [{"name": "bug"}],
            "repository_url": f"https://api.github.com/repos/{repo}", **extra}


def call(cid: str, inputs: dict[str, Any], key: str | None = None, target: Target | None = None) -> Invocation:
    return Invocation(cid, target, inputs, key, 10)


def github(cid: str, *replies: Response | Exception, secrets: Callable[[str], str | None] = SECRETS.get
           ) -> tuple[GitHubAdapter, Script]:
    script = Script(list(replies))
    return GitHubAdapter(f"capalis:github.{cid}", script, secrets), script


def slack(cid: str, *replies: Response | Exception) -> tuple[SlackAdapter, Script]:
    script = Script(list(replies))
    return SlackAdapter(f"capalis:slack.{cid}", script, SECRETS.get), script


def test_issue_create_embeds_the_key_and_maps_the_reply() -> None:
    adapter, script = github("issue.create", reply(201, issue(5, "report\n\n<!-- capalis:" + KEY + " -->")))
    result = adapter.invoke(call(adapter.capability_id, {"repo": REPO, "title": "t", "body": "report"}, KEY))
    assert result.outcome is Outcome.OK
    assert result.output == {"number": 5, "title": "t", "state": "open", "state_reason": "",
                             "html_url": f"https://github.com/{REPO}/issues/5",
                             "body": f"report\n\n<!-- capalis:{KEY} -->", "labels": ["bug"]}
    [sent] = script.sent
    assert sent.method == "POST" and sent.url == f"https://api.github.com/repos/{REPO}/issues"
    assert sent.body["body"].endswith(f"<!-- capalis:{KEY} -->")
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(("response", "outcome"), [
    (reply(422, {"message": "Validation Failed"}), Outcome.REJECTED),
    (reply(404, {"message": "Not Found"}), Outcome.REJECTED),
    (reply(403, {"message": "rate limit"}, x_ratelimit_remaining="0", retry_after="30"), Outcome.RETRYABLE),
    (reply(429, {}), Outcome.RETRYABLE),
    (reply(502, {}), Outcome.AMBIGUOUS),
    (Response(201, {}, b"<html>"), Outcome.AMBIGUOUS),
    (TransportError("ConnectionRefusedError", sent=False), Outcome.RETRYABLE),
    (TransportError("timed out", sent=True), Outcome.AMBIGUOUS),
])
def test_issue_create_failures_are_classified_for_the_runner(response: Response | Exception,
                                                             outcome: Outcome) -> None:
    adapter, _ = github("issue.create", response)
    result = adapter.invoke(call(adapter.capability_id, {"repo": REPO, "title": "t", "body": "b"}, KEY))
    assert result.outcome is outcome
    assert TOKEN not in (result.error or "")


def test_reads_never_report_ambiguity() -> None:
    adapter, _ = github("issue.get", reply(503, {}))
    assert adapter.invoke(call(adapter.capability_id, {"repo": REPO, "number": 1})).outcome is Outcome.RETRYABLE


def test_search_quotes_the_phrase_and_keeps_to_the_target_repo() -> None:
    adapter, script = github("issue.search", reply(200, {"total_count": 3, "items": [
        issue(1, "has m-1"), issue(2, "has m-1", repo="attacker/repo"), issue(3, "has m-1", pull_request={})]}))
    result = adapter.invoke(call(adapter.capability_id, {"repo": REPO, "query": 'm-1" repo:attacker/repo'}))
    assert result.outcome is Outcome.OK and result.output is not None
    assert [i["number"] for i in result.output["items"]] == [1] and result.output["total_count"] == 1
    query = urllib.parse.parse_qs(urllib.parse.urlparse(script.sent[0].url).query)["q"][0]
    assert query == f'repo:{REPO} is:issue "m-1  repo:attacker/repo"'


def test_find_by_key_scans_the_consistent_list_and_matches_exactly() -> None:
    other = "cap-" + "b" * 32
    adapter, script = github("issue.find_by_key", reply(200, [
        issue(9, f"<!-- capalis:{other} -->"), issue(8, "x", pull_request={}), issue(7, f"r\n<!-- capalis:{KEY} -->")]))
    result = adapter.invoke(call(adapter.capability_id, {"repo": REPO, "idempotency_key": KEY, "since": 1_800_000_000}))
    assert result.output is not None and result.output["found"] and result.output["resource"]["number"] == 7
    assert "/issues?state=all" in script.sent[0].url and "since=2027-01-15T07%3A55%3A00Z" in script.sent[0].url

    adapter, _ = github("issue.find_by_key", reply(200, [issue(9, f"<!-- capalis:{other} -->")]))
    assert adapter.invoke(call(adapter.capability_id, {"repo": REPO, "idempotency_key": KEY})).output == {
        "found": False}


def test_find_by_key_never_says_not_found_when_it_could_not_look_everywhere() -> None:
    page = [issue(n) for n in range(100)]
    adapter, _ = github("issue.find_by_key", *[reply(200, page) for _ in range(10)])
    result = adapter.invoke(call(adapter.capability_id, {"repo": REPO, "idempotency_key": KEY}))
    assert result.outcome is Outcome.RETRYABLE


def test_get_refuses_pull_requests_and_missing_token_never_calls_out() -> None:
    adapter, _ = github("issue.get", reply(200, issue(3, pull_request={})))
    assert adapter.invoke(call(adapter.capability_id, {"repo": REPO, "number": 3})).outcome is Outcome.REJECTED
    adapter, script = github("issue.get", secrets=lambda _: None)
    result = adapter.invoke(call(adapter.capability_id, {"repo": REPO, "number": 3}))
    assert result.outcome is Outcome.REJECTED and GITHUB_TOKEN in (result.error or "") and script.sent == []


def test_slack_post_carries_the_key_as_message_metadata() -> None:
    adapter, script = slack("message.post", reply(200, {"ok": True, "ts": "1.2", "channel": "C0CFU6F0MNK"}))
    result = adapter.invoke(call(adapter.capability_id, {"channel": "C0CFU6F0MNK", "text": "hi"}, KEY))
    assert result.output == {"ts": "1.2", "channel": "C0CFU6F0MNK"}
    assert script.sent[0].body["metadata"] == {"event_type": "capalis_effect", "event_payload": {"key": KEY}}


@pytest.mark.parametrize(("data", "outcome"), [
    ({"ok": False, "error": "not_in_channel"}, Outcome.REJECTED),
    ({"ok": False, "error": "ratelimited"}, Outcome.RETRYABLE),
    ({"ok": False, "error": "internal_error"}, Outcome.AMBIGUOUS),
    ({"ok": True}, Outcome.AMBIGUOUS),
])
def test_slack_post_errors(data: dict[str, Any], outcome: Outcome) -> None:
    adapter, _ = slack("message.post", reply(200, data))
    assert adapter.invoke(call(adapter.capability_id, {"channel": "C1", "text": "hi"}, KEY)).outcome is outcome


def test_slack_find_and_get() -> None:
    history = {"ok": True, "has_more": False, "messages": [
        {"ts": "3.0", "text": "x", "metadata": {"event_type": "capalis_effect", "event_payload": {"key": "other"}}},
        {"ts": "2.0", "text": "y", "metadata": {"event_type": "capalis_effect", "event_payload": {"key": KEY}}}]}
    adapter, script = slack("message.find_by_key", reply(200, history))
    result = adapter.invoke(call(adapter.capability_id, {"channel": "C1", "idempotency_key": KEY, "since": 100}))
    assert result.output == {"found": True, "resource": {"ts": "2.0", "channel": "C1"}}
    assert "include_all_metadata=true" in script.sent[0].url and "oldest=0" in script.sent[0].url

    adapter, _ = slack("message.get", reply(200, {"ok": True, "messages": [{"ts": "2.0", "text": "New bug"}]}))
    assert adapter.invoke(call(adapter.capability_id, {"channel": "C1", "ts": "2.0"})).output == {
        "text": "New bug", "ts": "2.0"}
    adapter, _ = slack("message.get", reply(200, {"ok": True, "messages": []}))
    assert adapter.invoke(call(adapter.capability_id, {"channel": "C1", "ts": "2.0"})).outcome is Outcome.REJECTED


def test_webhook_only_posts_to_slack_hooks() -> None:
    adapter, script = slack("webhook.post", Response(200, {}, b"ok"))
    assert adapter.invoke(call(adapter.capability_id, {"channel": "C1", "text": "hi"})).output == {"ok": True}
    assert script.sent[0].url == SECRETS[SLACK_WEBHOOK_URL]
    evil = SlackAdapter("capalis:slack.webhook.post", Script([]), {SLACK_WEBHOOK_URL: "https://evil.test/x"}.get)
    assert evil.invoke(call(evil.capability_id, {"channel": "C1", "text": "hi"})).outcome is Outcome.REJECTED


def test_http_get_stays_on_the_approved_origin() -> None:
    script = Script([Response(200, {}, b"v1.2.3")])
    adapter = HttpGetAdapter(script)
    origin = Target("http.origin", "https://shop.example.com")
    ok = adapter.invoke(call(adapter.capability_id, {"origin": origin.value, "path": "/version"}, target=origin))
    assert ok.output == {"status": 200, "body_text": "v1.2.3"}
    for path in ("//evil.test/x",):
        bad = adapter.invoke(call(adapter.capability_id, {"origin": origin.value, "path": path}, target=origin))
        assert bad.outcome is Outcome.REJECTED
    other = adapter.invoke(call(adapter.capability_id, {"origin": "https://evil.test", "path": "/"}, target=origin))
    assert other.outcome is Outcome.REJECTED and len(script.sent) == 1


def test_transport_refuses_plain_http() -> None:
    with pytest.raises(TransportError) as info:
        UrllibTransport().send("GET", "http://example.com/", {}, None, 1)
    assert info.value.sent is False


def test_inventory_reports_availability_from_configured_secrets() -> None:
    adapters = inventory.build(secret={GITHUB_TOKEN: TOKEN}.get, transport=Script([]))
    caps = inventory.descriptors(adapters)
    assert set(caps) == {cap.id for cap in CATALOG}
    assert caps["capalis:github.issue.create"].availability is Availability.AVAILABLE
    assert caps["capalis:slack.message.post"].availability is Availability.NEEDS_AUTH
    assert caps["capalis:http.get"].availability is Availability.AVAILABLE


DISCORD_TOKEN = "discord-token-" + "z" * 20
CHANNEL, BOT, REPORT = "1569488960479297590", "1569487656465727660", "1569637681166811227"


def discord(cid: str, *replies: Response | Exception) -> tuple[Any, Script]:
    from capalis.adapters.catalog import DISCORD_BOT_TOKEN
    from capalis.adapters.discord import DiscordAdapter

    script = Script(list(replies))
    return DiscordAdapter(f"capalis:discord.{cid}", script, {DISCORD_BOT_TOKEN: DISCORD_TOKEN}.get), script


def test_discord_reply_disables_mentions_and_sends_a_key_derived_nonce() -> None:
    from capalis.adapters.discord import nonce_for

    adapter, script = discord("message.post", reply(200, {"id": "1569700000000000001", "channel_id": CHANNEL,
                                                          "content": "Thanks"}))
    inputs = {"channel": CHANNEL, "text": "Thanks", "reply_to": REPORT}
    result = adapter.invoke(call(adapter.capability_id, inputs, KEY))
    assert result.output == {"id": "1569700000000000001", "channel": CHANNEL, "text": "Thanks"}
    sent = script.sent[0]
    assert sent.url == f"https://discord.com/api/v10/channels/{CHANNEL}/messages"
    assert sent.headers["Authorization"] == f"Bot {DISCORD_TOKEN}"
    assert sent.body["allowed_mentions"] == {"parse": []}
    assert sent.body["message_reference"]["message_id"] == REPORT
    assert sent.body["nonce"] == nonce_for(KEY) and len(sent.body["nonce"]) <= 25 and sent.body["enforce_nonce"]


@pytest.mark.parametrize(("response", "outcome"), [
    (reply(403, {"code": 50013, "message": "Missing Permissions"}), Outcome.REJECTED),
    (reply(429, {"retry_after": 1.5}, retry_after="2"), Outcome.RETRYABLE),
    (reply(500, {}), Outcome.AMBIGUOUS),
    (TransportError("timed out", sent=True), Outcome.AMBIGUOUS),
])
def test_discord_post_failures(response: Response | Exception, outcome: Outcome) -> None:
    adapter, _ = discord("message.post", response)
    result = adapter.invoke(call(adapter.capability_id, {"channel": CHANNEL, "text": "x"}, KEY))
    assert result.outcome is outcome and DISCORD_TOKEN not in (result.error or "")


def test_discord_lookup_matches_only_this_bots_reply_with_the_same_text() -> None:
    page = [
        {"id": "1569700000000000003", "content": "Thanks", "author": {"id": "999"},
         "message_reference": {"message_id": REPORT}},
        {"id": "1569700000000000002", "content": "Thanks", "author": {"id": BOT},
         "message_reference": {"message_id": "1569637681166811999"}},
        {"id": "1569700000000000001", "content": "Thanks", "author": {"id": BOT}, "channel_id": CHANNEL,
         "message_reference": {"message_id": REPORT}},
    ]
    adapter, script = discord("message.find_by_key", reply(200, {"id": BOT}), reply(200, page))
    inputs = {"channel": CHANNEL, "idempotency_key": KEY, "text": "Thanks", "reply_to": REPORT, "since": 1_794_300_000}
    result = adapter.invoke(call(adapter.capability_id, inputs))
    assert result.output == {"found": True, "resource": {"id": "1569700000000000001", "channel": CHANNEL,
                                                         "text": "Thanks"}}
    assert "after=" in script.sent[1].url and "after=0" not in script.sent[1].url

    adapter, _ = discord("message.find_by_key", reply(200, {"id": BOT}), reply(200, page[:2]))
    assert adapter.invoke(call(adapter.capability_id, inputs)).output == {"found": False}
    adapter, _ = discord("message.find_by_key", reply(200, {"id": BOT}), *[reply(200, [
        {"id": str(1569700000000000100 + n), "content": "x", "author": {"id": "1"}} for n in range(100)])] * 10)
    assert adapter.invoke(call(adapter.capability_id, inputs)).outcome is Outcome.RETRYABLE


def test_discord_get_reads_one_message() -> None:
    adapter, script = discord("message.get", reply(200, {"id": "1569700000000000001", "channel_id": CHANNEL,
                                                         "content": "closed as fixed"}))
    result = adapter.invoke(call(adapter.capability_id, {"channel": CHANNEL, "id": "1569700000000000001"}))
    assert result.output == {"id": "1569700000000000001", "channel": CHANNEL, "text": "closed as fixed"}
    assert script.sent[0].url.endswith(f"/channels/{CHANNEL}/messages/1569700000000000001")
