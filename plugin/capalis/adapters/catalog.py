"""Descriptors for the capabilities Capalis implements and has reviewed itself.

Only these can run in managed Recipes (D-018). Availability is not part of a descriptor's identity: it
is filled in at inventory time from whether the credential is configured.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .. import __version__
from ..core.model import Availability, CapabilityDescriptor, Effect, IdempotencySupport, TargetSpec, Trust

REVIEWED_AT = "2026-11-10"
GITHUB_TOKEN = "CAPALIS_GITHUB_TOKEN"
SLACK_BOT_TOKEN = "CAPALIS_SLACK_BOT_TOKEN"
SLACK_WEBHOOK_URL = "CAPALIS_SLACK_WEBHOOK_URL"
DISCORD_BOT_TOKEN = "CAPALIS_DISCORD_BOT_TOKEN"

_STR = {"type": "string"}
_INT = {"type": "integer"}


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


# state_reason: "completed", "not_planned", "duplicate" or "" while open.
ISSUE = _obj({"number": _INT, "title": _STR, "state": _STR, "state_reason": _STR, "html_url": _STR, "body": _STR,
              "labels": {"type": "array", "items": _STR}}, ["number", "title", "state", "html_url"])
MESSAGE = _obj({"ts": _STR, "channel": _STR}, ["ts", "channel"])
DISCORD_MESSAGE = _obj({"id": _STR, "channel": _STR, "text": _STR}, ["id", "channel", "text"])
_SNOWFLAKE = {"type": "string", "pattern": "^[0-9]{17,20}$"}
_SLACK_TS = {"type": "string", "pattern": "^[0-9]{9,11}\\.[0-9]{6}$"}
_REPO = TargetSpec("repo", "github.repo")
_CHANNEL = TargetSpec("channel", "slack.channel")
_DISCORD_CHANNEL = TargetSpec("channel", "discord.channel")


def _cap(cid: str, *, summary: str, effect: Effect, inputs: dict[str, Any], required: list[str],
         output: dict[str, Any], target: TargetSpec, auth: str | None, operation: str,
         idempotency: IdempotencySupport = IdempotencySupport.NONE, reconcile_with: str | None = None,
         readback: tuple[str, ...] = (), limits: tuple[str, ...] = (), setup_hint: str = "") -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id=cid, source="capalis", provider=cid.split(":")[1].split(".")[0], version=__version__,
        tool_name=cid.split(":", 1)[1], operation=operation, summary=summary,
        input_schema=_obj(inputs, required), output_schema=output, effect=effect, target=target,
        trust=Trust.REVIEWED, availability=Availability.UNKNOWN, idempotency=idempotency,
        reconcile_with=reconcile_with, auth_requirement=auth, observed_at=REVIEWED_AT, limits=limits,
        readback=readback, setup_hint=setup_hint,
    )


_GITHUB_SETUP = (f"Create a fine-grained GitHub token with Issues read/write on the repository and store it as "
                 f"{GITHUB_TOKEN}.")
_DISCORD_SETUP = (f"Store the Discord bot token as {DISCORD_BOT_TOKEN}; the bot needs View Channel, Send Messages "
                  "and Read Message History in the channel.")
_SLACK_SETUP = (f"Store a Slack bot token (chat:write, channels:history) as {SLACK_BOT_TOKEN} and invite the bot to "
                "the channel.")

CATALOG: tuple[CapabilityDescriptor, ...] = (
    _cap("capalis:github.issue.search", summary="Find issues in one repository whose text contains a phrase",
         effect=Effect.READ, inputs={"repo": _STR, "query": {"type": "string", "minLength": 1, "maxLength": 256}},
         required=["repo", "query"], output=_obj({"total_count": _INT, "items": {"type": "array", "items": ISSUE}},
                                                 ["total_count", "items"]),
         target=_REPO, auth=GITHUB_TOKEN, operation="issue.search", setup_hint=_GITHUB_SETUP,
         limits=("GitHub search is eventually consistent: issues created in the last minute may be missing",)),
    _cap("capalis:github.issue.create", summary="Open an issue in one repository", effect=Effect.EXTERNAL_WRITE,
         inputs={"repo": _STR, "title": {"type": "string", "minLength": 1, "maxLength": 256},
                 "body": {"type": "string", "maxLength": 60000}, "labels": {"type": "array", "items": _STR}},
         required=["repo", "title", "body"], output=ISSUE, target=_REPO, auth=GITHUB_TOKEN, operation="issue.create",
         idempotency=IdempotencySupport.RECONCILE, reconcile_with="capalis:github.issue.find_by_key",
         readback=("capalis:github.issue.get",), setup_hint=_GITHUB_SETUP),
    _cap("capalis:github.issue.find_by_key", summary="Find the issue an earlier attempt created, by its Capalis key",
         effect=Effect.READ, inputs={"repo": _STR, "idempotency_key": _STR, "since": _INT},
         required=["repo", "idempotency_key"], output=_obj({"found": {"type": "boolean"}, "resource": ISSUE},
                                                           ["found"]),
         target=_REPO, auth=GITHUB_TOKEN, operation="issue.find_by_key", setup_hint=_GITHUB_SETUP),
    _cap("capalis:github.issue.get", summary="Read one issue back from GitHub", effect=Effect.READ,
         inputs={"repo": _STR, "number": {"type": "integer", "minimum": 1}}, required=["repo", "number"],
         output=ISSUE, target=_REPO, auth=GITHUB_TOKEN, operation="issue.get", setup_hint=_GITHUB_SETUP),
    _cap("capalis:slack.message.post", summary="Post a message to one Slack channel as the Capalis bot",
         effect=Effect.EXTERNAL_SEND, inputs={"channel": _STR, "text": {"type": "string", "minLength": 1,
                                                                        "maxLength": 3000},
                                              "thread_ts": _SLACK_TS},
         required=["channel", "text"], output=MESSAGE, target=_CHANNEL, auth=SLACK_BOT_TOKEN, operation="message.post",
         idempotency=IdempotencySupport.RECONCILE, reconcile_with="capalis:slack.message.find_by_key",
         readback=("capalis:slack.message.get",), setup_hint=_SLACK_SETUP),
    _cap("capalis:slack.message.find_by_key", summary="Find the message an earlier attempt posted, by its Capalis key",
         effect=Effect.READ, inputs={"channel": _STR, "idempotency_key": _STR, "since": _INT, "thread_ts": _SLACK_TS},
         required=["channel", "idempotency_key"], output=_obj({"found": {"type": "boolean"}, "resource": MESSAGE},
                                                              ["found"]),
         target=_CHANNEL, auth=SLACK_BOT_TOKEN, operation="message.find_by_key", setup_hint=_SLACK_SETUP),
    _cap("capalis:slack.message.get", summary="Read one Slack message back", effect=Effect.READ,
         inputs={"channel": _STR, "ts": _STR, "thread_ts": _SLACK_TS}, required=["channel", "ts"],
         output=_obj({"text": _STR, "ts": _STR},
                                                                                     ["text", "ts"]),
         target=_CHANNEL, auth=SLACK_BOT_TOKEN, operation="message.get", setup_hint=_SLACK_SETUP),
    _cap("capalis:slack.webhook.post", summary="Post to the Slack channel an incoming webhook is bound to",
         effect=Effect.EXTERNAL_SEND, inputs={"channel": _STR, "text": {"type": "string", "minLength": 1,
                                                                        "maxLength": 3000}},
         required=["channel", "text"], output=_obj({"ok": {"type": "boolean"}}, ["ok"]), target=_CHANNEL,
         auth=SLACK_WEBHOOK_URL, operation="message.post",
         limits=("Incoming webhooks cannot read messages back, so posts cannot be independently verified",
                 "A timed-out post cannot be checked, so it is never retried automatically"),
         setup_hint=f"Create an incoming webhook for the channel and store its URL as {SLACK_WEBHOOK_URL}."),
    _cap("capalis:discord.message.post", summary="Post (or reply) in one Discord channel as the Capalis bot",
         effect=Effect.EXTERNAL_SEND, inputs={"channel": _STR, "text": {"type": "string", "minLength": 1,
                                                                        "maxLength": 1900},
                                              "reply_to": _SNOWFLAKE},
         required=["channel", "text"], output=DISCORD_MESSAGE, target=_DISCORD_CHANNEL, auth=DISCORD_BOT_TOKEN,
         operation="discord.message.post", idempotency=IdempotencySupport.RECONCILE,
         reconcile_with="capalis:discord.message.find_by_key", readback=("capalis:discord.message.get",),
         setup_hint=_DISCORD_SETUP,
         limits=("Mentions are disabled in posted text, so a message cannot ping people",)),
    _cap("capalis:discord.message.find_by_key",
         summary="Find the message an earlier attempt posted: this bot's reply to the same message with the same text",
         effect=Effect.READ, inputs={"channel": _STR, "idempotency_key": _STR, "since": _INT, "reply_to": _SNOWFLAKE,
                                     "text": _STR},
         required=["channel", "idempotency_key", "text"],
         output=_obj({"found": {"type": "boolean"}, "resource": DISCORD_MESSAGE}, ["found"]),
         target=_DISCORD_CHANNEL, auth=DISCORD_BOT_TOKEN, operation="discord.message.find_by_key",
         setup_hint=_DISCORD_SETUP),
    _cap("capalis:discord.message.get", summary="Read one Discord message back", effect=Effect.READ,
         inputs={"channel": _STR, "id": _SNOWFLAKE}, required=["channel", "id"], output=DISCORD_MESSAGE,
         target=_DISCORD_CHANNEL, auth=DISCORD_BOT_TOKEN, operation="discord.message.get", setup_hint=_DISCORD_SETUP),
    _cap("capalis:http.get", summary="Fetch one path from an approved HTTPS origin, without credentials",
         effect=Effect.READ, inputs={"origin": _STR, "path": {"type": "string", "pattern": "^/", "maxLength": 512}},
         required=["origin", "path"], output=_obj({"status": _INT, "body_text": _STR}, ["status", "body_text"]),
         target=TargetSpec("origin", "http.origin"), auth=None, operation="http.get"),
)

BY_ID = {cap.id: cap for cap in CATALOG}


def with_availability(cap: CapabilityDescriptor, has_secret: bool) -> CapabilityDescriptor:
    state = Availability.AVAILABLE if cap.auth_requirement is None or has_secret else Availability.NEEDS_AUTH
    return replace(cap, availability=state)
