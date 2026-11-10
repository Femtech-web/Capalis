from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

import pytest

from capalis.adapters.catalog import DISCORD_BOT_TOKEN, GITHUB_TOKEN, SLACK_BOT_TOKEN
from capalis.adapters.directory import Directory, ResolveError, readiness
from capalis.adapters.http import Response
from capalis.adapters.render import setup

SECRETS = {GITHUB_TOKEN: "ghp_" + "x" * 36, SLACK_BOT_TOKEN: "xoxb-" + "y" * 30, DISCORD_BOT_TOKEN: "d" * 40}


def ok(data: Any, status: int = 200) -> Response:
    return Response(status, {}, json.dumps(data).encode())


@dataclass
class Routes:
    """Answers by URL substring, so tests describe the provider rather than the call order."""

    routes: dict[str, Response | list[Response]]
    seen: list[str] = field(default_factory=list)

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float) -> Response:
        self.seen.append(url)
        for fragment, reply in self.routes.items():
            if fragment in url:
                return reply.pop(0) if isinstance(reply, list) else reply
        return ok({"message": "not found"}, 404)


def directory(routes: dict[str, Response | list[Response]], secrets: dict[str, str] = SECRETS) -> Directory:
    return Directory(Routes(routes), secrets.get)


def test_slack_channel_by_name_pages_and_flags_missing_membership() -> None:
    d = directory({"conversations.list": [
        ok({"ok": True, "channels": [{"id": "C0AAAAAAAA1", "name": "general", "is_member": True}],
            "response_metadata": {"next_cursor": "c2"}}),
        ok({"ok": True, "channels": [{"id": "C0CFU6F0MNK", "name": "engineering", "is_member": False}]})]})
    hit = d.slack_channel("#Engineering")
    assert (hit.value, hit.label) == ("C0CFU6F0MNK", "#engineering") and "not in this channel" in hit.note
    with pytest.raises(ResolveError, match="no Slack channel named #random"):
        directory({"conversations.list": ok({"ok": True, "channels": []})}).slack_channel("random")


def test_slack_channel_by_id_reads_it_back() -> None:
    d = directory({"conversations.info": ok({"ok": True, "channel": {"id": "C0CFU6F0MNK", "name": "engineering",
                                                                     "is_member": True}})})
    assert d.slack_channel("C0CFU6F0MNK").label == "#engineering"


def test_discord_channel_by_name_across_servers() -> None:
    guilds = ok([{"id": "1", "name": "Capalis Test"}, {"id": "2", "name": "Other"}])
    d = directory({"/users/@me/guilds": guilds, "/guilds/1/channels": ok([
        {"id": "1569488960479297590", "name": "bugs", "type": 0}, {"id": "9", "name": "General", "type": 2}]),
        "/guilds/2/channels": ok([{"id": "8", "name": "random", "type": 0}])})
    hit = d.discord_channel("#bugs")
    assert (hit.value, hit.label, hit.note) == ("1569488960479297590", "#bugs", "in Capalis Test")

    twice = directory({"/users/@me/guilds": guilds, "/guilds/1/channels": ok([{"id": "1", "name": "bugs", "type": 0}]),
                       "/guilds/2/channels": ok([{"id": "2", "name": "bugs", "type": 0}])})
    with pytest.raises(ResolveError, match="several servers"):
        twice.discord_channel("bugs")


def test_discord_user_falls_back_to_recent_authors_without_the_members_intent() -> None:
    d = directory({"/channels/1569488960479297590/messages": ok([
        {"author": {"id": "1145236794108035144", "username": "oluwafemi", "global_name": "Oluwafemi Akolade"}}]),
        "/channels/1569488960479297590": ok({"id": "1569488960479297590", "name": "bugs", "guild_id": "1"}),
        "/members/search": ok({"message": "Missing Access"}, 403)})
    hit = d.discord_user("@oluwafemi", "1569488960479297590")
    assert (hit.value, hit.label) == ("1145236794108035144", "@Oluwafemi Akolade")
    assert d.discord_user("akolade", "1569488960479297590").value == "1145236794108035144"
    with pytest.raises(ResolveError, match="Ask them to post once"):
        d.discord_user("@nobody", "1569488960479297590")


def test_github_repo_by_bare_name_or_full_name() -> None:
    d = directory({"/user/repos": ok([{"name": "capalis-sandbox", "full_name": "oluwafemi-akolade/capalis-sandbox"}]),
                   "/repos/oluwafemi-akolade/capalis-sandbox": ok({"full_name": "oluwafemi-akolade/capalis-sandbox"})})
    assert d.github_repo("capalis-sandbox").value == "oluwafemi-akolade/capalis-sandbox"
    assert d.github_repo("https://github.com/oluwafemi-akolade/capalis-sandbox").value == \
        "oluwafemi-akolade/capalis-sandbox"
    with pytest.raises(ResolveError, match="cannot see"):
        directory({}).github_repo("someone/private")


def test_missing_token_is_reported_not_raised_as_a_crash() -> None:
    with pytest.raises(ResolveError, match="run /capalis setup"):
        directory({}, secrets={}).slack_channel("#engineering")


def test_setup_report_names_each_gap_and_its_fix() -> None:
    d = directory({"api.github.com/user": ok({"login": "oluwafemi-akolade"}),
                   "auth.test": ok({"ok": True, "user": "hermes", "team": "Capalis Test"}),
                   "conversations.info": ok({"ok": True, "channel": {"id": "C0CFU6F0MNK", "name": "engineering",
                                                                     "is_member": False}})},
                  secrets={GITHUB_TOKEN: "t", SLACK_BOT_TOKEN: "t"})
    checks = readiness(d, {"slack.channel:C0CFU6F0MNK"}, {"1569488960479297590"})
    text = setup([asdict(c) for c in checks])
    assert "OK   GitHub: connected as oluwafemi-akolade" in text
    assert "OK   Slack: connected as hermes in Capalis Test" in text
    assert "FIX  Discord: CAPALIS_DISCORD_BOT_TOKEN is not set" in text
    assert "FIX  Slack: #engineering: the Capalis bot is not in this channel yet" in text
    assert "/invite @" in text and "DISCORD_FREE_RESPONSE_CHANNELS" in text
    assert "2 thing(s) to fix." in text


def test_partial_names_must_be_unambiguous() -> None:
    authors = [{"author": {"id": "1", "username": "ada_l", "global_name": "Ada Lovelace"}},
               {"author": {"id": "2", "username": "adam", "global_name": "Adam Smith"}},
               {"author": {"id": "3", "username": "ada", "global_name": None}}]
    d = directory({"/channels/9/messages": ok(authors), "/channels/9": ok({"id": "9", "name": "bugs", "guild_id": "1"}),
                   "/members/search": ok({}, 403)})
    assert d.discord_user("@ada", "9").value == "3", "an exact match wins over prefixes"
    with pytest.raises(ResolveError, match="several people"):
        d.discord_user("@ad", "9")


def test_slack_user_by_display_or_real_name() -> None:
    members = {"ok": True, "members": [
        {"id": "U0ADA00001", "name": "ada", "real_name": "Ada Lovelace", "profile": {"display_name": "Ada"}},
        {"id": "U0BOT00001", "name": "bot", "is_bot": True, "profile": {}},
        {"id": "U0SAM00001", "name": "sam", "real_name": "Sam Smith", "profile": {"display_name": ""}}]}
    d = directory({"users.list": ok(members)})
    assert (d.slack_user("@lovelace").value, d.slack_user("Ada").label) == ("U0ADA00001", "@Ada")
    with pytest.raises(ResolveError, match="no Slack user"):
        d.slack_user("@bot")
