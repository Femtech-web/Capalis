"""Turn the names people use ("#bugs", "capalis-sandbox", "@ada") into the ids Recipes are pinned to.

Lookups are read-only. Recipes still store ids, because names can be renamed or reused; the name is
kept beside the id only for display.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .catalog import DISCORD_BOT_TOKEN, GITHUB_TOKEN, SLACK_BOT_TOKEN
from .http import Response, Transport, TransportError

SLACK_ID = re.compile(r"[CGD][A-Z0-9]{8,12}")
SLACK_USER = re.compile(r"[UW][A-Z0-9]{8,12}")
SNOWFLAKE = re.compile(r"[0-9]{17,20}")
REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
MAX_PAGES = 10


class ResolveError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Resolved:
    value: str
    label: str
    note: str = ""


def _clean(name: str, prefix: str) -> str:
    return name.strip().removeprefix(prefix).strip().lower()


@dataclass
class Directory:
    transport: Transport
    secret: Callable[[str], str | None]

    # -- transport helpers --------------------------------------------------------------------------

    def _get(self, url: str, headers: Mapping[str, str]) -> Response:
        try:
            return self.transport.send("GET", url, headers, None, 15)
        except TransportError as exc:
            raise ResolveError(f"could not reach {urllib.parse.urlparse(url).hostname}: {exc}") from exc

    def _token(self, name: str) -> str:
        token = self.secret(name)
        if not token:
            raise ResolveError(f"{name} is not configured; run /capalis setup")
        return token

    def _slack(self, method: str, **query: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token(SLACK_BOT_TOKEN)}"}
        response = self._get(f"https://slack.com/api/{method}?{urllib.parse.urlencode(query)}", headers)
        data = response.json() or {}
        if not data.get("ok"):
            raise ResolveError(f"Slack refused {method}: {data.get('error') or response.status}")
        return dict(data)

    def _discord(self, path: str) -> Any:
        headers = {"Authorization": f"Bot {self._token(DISCORD_BOT_TOKEN)}"}
        response = self._get("https://discord.com/api/v10" + path, headers)
        if response.status == 403:
            raise PermissionError(path)
        if not 200 <= response.status < 300:
            raise ResolveError(f"Discord returned {response.status} for {path.split('?')[0]}")
        return response.json()

    def _github(self, path: str) -> Response:
        headers = {"Authorization": f"Bearer {self._token(GITHUB_TOKEN)}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        return self._get("https://api.github.com" + path, headers)

    # -- lookups ------------------------------------------------------------------------------------

    def slack_channel(self, name: str) -> Resolved:
        if SLACK_ID.fullmatch(name.strip()):
            info = self._slack("conversations.info", channel=name.strip())["channel"]
            return Resolved(info["id"], f"#{info.get('name', info['id'])}",
                            "" if info.get("is_member") else "the Capalis bot is not in this channel yet")
        wanted = _clean(name, "#")
        query: dict[str, Any] = {"types": "public_channel,private_channel", "exclude_archived": "true",
                                 "limit": 200}
        for _ in range(MAX_PAGES):
            data = self._slack("conversations.list", **query)
            for channel in data.get("channels", []):
                if str(channel.get("name", "")).lower() == wanted:
                    return Resolved(channel["id"], f"#{channel['name']}",
                                    "" if channel.get("is_member") else "the Capalis bot is not in this channel yet")
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
            query["cursor"] = cursor
        raise ResolveError(f"no Slack channel named #{wanted} that the Capalis bot can see")

    def slack_user(self, name: str) -> Resolved:
        if SLACK_USER.fullmatch(name.strip()):
            user = self._slack("users.info", user=name.strip())["user"]
            return Resolved(user["id"], f"@{(user.get('profile') or {}).get('display_name') or user.get('name')}")
        wanted = _clean(name, "@")

        def names_of(user: Mapping[str, Any]) -> set[str]:
            profile = user.get("profile") or {}
            return {str(v or "").lower() for v in (user.get("name"), user.get("real_name"),
                                                   profile.get("display_name"), profile.get("real_name"))} - {""}

        users: list[Mapping[str, Any]] = []
        query: dict[str, Any] = {"limit": 200}
        for _ in range(MAX_PAGES):
            data = self._slack("users.list", **query)
            users += [u for u in data.get("members", []) if not u.get("deleted") and not u.get("is_bot")]
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
            query["cursor"] = cursor
        exact = {u["id"]: u for u in users if wanted in names_of(u)}
        loose = {u["id"]: u for u in users
                 if any(n.startswith(wanted) or any(w.startswith(wanted) for w in n.split()) for n in names_of(u))}
        found = exact or loose
        if len(found) == 1:
            user = next(iter(found.values()))
            return Resolved(str(user["id"]), f"@{(user.get('profile') or {}).get('display_name') or user.get('name')}")
        if found:
            raise ResolveError(f"several Slack users match @{wanted}; give their member id")
        raise ResolveError(f"no Slack user named @{wanted}")

    def discord_channel(self, name: str) -> Resolved:
        if SNOWFLAKE.fullmatch(name.strip()):
            try:
                channel = self._discord(f"/channels/{name.strip()}")
            except PermissionError as exc:
                raise ResolveError("the Capalis bot cannot see that Discord channel") from exc
            return Resolved(str(channel["id"]), f"#{channel.get('name', channel['id'])}")
        wanted = _clean(name, "#")
        found: list[tuple[str, dict[str, Any]]] = []
        for guild in self._discord("/users/@me/guilds")[:20]:
            try:
                channels = self._discord(f"/guilds/{guild['id']}/channels")
            except PermissionError:
                continue
            found += [(str(guild.get("name", "")), c) for c in channels
                      if c.get("type") in (0, 5) and str(c.get("name", "")).lower() == wanted]
        if not found:
            raise ResolveError(f"no Discord text channel named #{wanted} in the servers the Capalis bot is in")
        if len(found) > 1:
            raise ResolveError(f"#{wanted} exists in several servers ({', '.join(g for g, _ in found)}); "
                               "give the channel id instead")
        guild_name, channel = found[0]
        return Resolved(str(channel["id"]), f"#{channel['name']}", f"in {guild_name}")

    def discord_user(self, name: str, channel_id: str) -> Resolved:
        if SNOWFLAKE.fullmatch(name.strip()):
            try:
                user = self._discord(f"/users/{name.strip()}")
                return Resolved(str(user["id"]), f"@{user.get('global_name') or user.get('username')}")
            except (PermissionError, ResolveError):
                return Resolved(name.strip(), name.strip())
        wanted = _clean(name, "@")

        def names_of(user: Mapping[str, Any]) -> set[str]:
            return {str(user.get(k) or "").lower() for k in ("username", "global_name")} - {""}

        def matches(user: Mapping[str, Any]) -> bool:
            # Exact, or the start of a name or of one of its words ("@ada" for "Ada Lovelace"); a match that
            # is not unique is refused below, and the person is shown by name before anything is approved.
            return any(n == wanted or n.startswith(wanted) or any(w.startswith(wanted) for w in n.split())
                       for n in names_of(user))

        channel = self._discord(f"/channels/{channel_id}")
        try:
            # Needs the Server Members intent; without it, fall back to recent authors in the channel.
            members = self._discord(f"/guilds/{channel['guild_id']}/members/search?"
                                    f"{urllib.parse.urlencode({'query': wanted, 'limit': 10})}")
            users = [m["user"] for m in members if matches(m.get("user") or {})]
        except (PermissionError, ResolveError, KeyError):
            users = [m["author"] for m in self._discord(f"/channels/{channel_id}/messages?limit=100")
                     if matches(m.get("author") or {})]
        exact = {str(u["id"]): u for u in users if wanted in names_of(u)}
        unique = exact or {str(u["id"]): u for u in users}
        if len(unique) == 1:
            user = next(iter(unique.values()))
            return Resolved(str(user["id"]), f"@{user.get('global_name') or user.get('username')}")
        if len(unique) > 1:
            raise ResolveError(f"several people match @{wanted}; give their Discord user id")
        raise ResolveError(f"could not find @{wanted} in #{channel.get('name', channel_id)}. Ask them to post once "
                           "in that channel, or give their user id (Discord: Developer Mode on, right-click the "
                           "name, Copy User ID)")

    def github_repo(self, name: str) -> Resolved:
        cleaned = name.strip().removeprefix("https://github.com/").strip("/")
        if REPO.fullmatch(cleaned):
            response = self._github(f"/repos/{cleaned}")
            if response.status == 404:
                raise ResolveError(f"the GitHub token cannot see {cleaned}")
            if response.status != 200:
                raise ResolveError(f"GitHub returned {response.status} for {cleaned}")
            data = response.json() or {}
            return Resolved(str(data.get("full_name") or cleaned), str(data.get("full_name") or cleaned))
        wanted = cleaned.lower()
        hits = []
        for page in range(1, MAX_PAGES + 1):
            response = self._github(f"/user/repos?per_page=100&page={page}")
            if response.status != 200:
                raise ResolveError(f"GitHub returned {response.status} listing repositories")
            repos = response.json() or []
            hits += [r["full_name"] for r in repos if str(r.get("name", "")).lower() == wanted]
            if len(repos) < 100:
                break
        if len(hits) == 1:
            return Resolved(hits[0], hits[0])
        if hits:
            raise ResolveError(f"several repositories are named {cleaned}: {', '.join(hits)}")
        raise ResolveError(f"no repository named {cleaned} that the GitHub token can see")


def label_key(kind: str, value: str) -> str:
    return f"{kind}:{value}"


@dataclass(frozen=True, slots=True)
class Check:
    area: str
    ok: bool | None
    detail: str
    fix: str = ""


def readiness(directory: Directory, targets: set[str], discord_triggers: set[str],
              slack_triggers: Iterable[str] = ()) -> list[Check]:
    """Plain-language setup check: credentials, identities, and access to every target in use."""
    from .catalog import DISCORD_BOT_TOKEN, GITHUB_TOKEN, SLACK_BOT_TOKEN

    checks: list[Check] = []
    settings_fix = "add it in Hermes Desktop (Settings > Plugins > Capalis) or run: hermes config set {name} <value>"
    for area, name, probe in (
            ("GitHub", GITHUB_TOKEN, lambda: directory._github("/user")),
            ("Slack", SLACK_BOT_TOKEN, lambda: directory._slack("auth.test")),
            ("Discord", DISCORD_BOT_TOKEN, lambda: directory._discord("/users/@me"))):
        if not directory.secret(name):
            checks.append(Check(area, False, f"{name} is not set", settings_fix.format(name=name)))
            continue
        try:
            reply: Any = probe()
            if isinstance(reply, Response):
                if reply.status != 200:
                    raise ResolveError(f"GitHub returned {reply.status}")
                who = (reply.json() or {}).get("login", "")
            elif area == "Slack":
                who = f"{reply.get('user')} in {reply.get('team')}"
            else:
                who = str(reply.get("username", ""))
            checks.append(Check(area, True, f"connected as {who}"))
        except (ResolveError, PermissionError) as exc:
            checks.append(Check(area, False, f"{name} did not work: {exc}", "create a new token and store it again"))
    for target in sorted(targets):
        kind, _, value = target.partition(":")
        try:
            if kind == "github.repo":
                hit = directory.github_repo(value)
                checks.append(Check("GitHub", True, f"can see {hit.label}"))
            elif kind == "slack.channel":
                hit = directory.slack_channel(value)
                checks.append(Check("Slack", not hit.note, f"{hit.label}: " + (hit.note or "the bot is a member"),
                                    f"in Slack, open {hit.label} and type /invite @ followed by the bot's name"
                                    if hit.note else ""))
            elif kind == "discord.channel":
                hit = directory.discord_channel(value)
                checks.append(Check("Discord", True, f"can see {hit.label}"))
        except (ResolveError, PermissionError) as exc:
            checks.append(Check(kind.split(".")[0].title(), False, f"{value}: {exc}",
                                "give the Capalis bot access to it, or pick another target"))
    for channel in sorted(slack_triggers):
        try:
            shown = f"{directory.slack_channel(channel).label} ({channel})"
        except (ResolveError, PermissionError):
            shown = channel
        checks.append(Check("Hermes gateway", None,
                            f"Slack messages in {shown} reach Capalis only if the gateway lists it",
                            f"set SLACK_FREE_RESPONSE_CHANNELS to include {channel} and add reporters to "
                            "SLACK_ALLOWED_USERS, then restart the gateway"))
    for channel in sorted(discord_triggers):
        try:
            shown = f"{directory.discord_channel(channel).label} ({channel})"
        except (ResolveError, PermissionError):
            shown = channel
        checks.append(Check("Hermes gateway", None,
                            f"Discord messages in {shown} reach Capalis only if the gateway lists it",
                            f"set DISCORD_FREE_RESPONSE_CHANNELS to include {channel} and add reporters to "
                            "DISCORD_ALLOWED_USERS, then restart the gateway"))
    return checks
