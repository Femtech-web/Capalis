"""Starting drafts the model adapts. They compile only against configured, reviewed capabilities."""

from __future__ import annotations

from typing import Any

DRAFT_FORMAT = (
    "Draft format: {intent, trigger, steps, budgets?, approval?}. trigger: {kind: event|manual|schedule, "
    "source: 'discord:channel:<id>' for events, senders: [user ids allowed to trigger], payload_schema "
    "(JSON Schema object with additionalProperties false; Discord events carry message_id, author, text), "
    "dedupe_key: 'message_id', max_runs_per_hour}. Each step: {id, capability, inputs, when?, after?, "
    "retry?, verify? | unverifiable_reason?}. Inputs are {const: value} (targets such as repo or channel "
    "must be const), {from: 'trigger.text' | 'steps.<id>.<field>'}, {template: 'text {trigger.author}'} or "
    "{first_of: [refs]}. when: {op: equals|not_equals|exists|missing|truthy|in, ref, value}. Every write "
    "needs verify: [{id, probe: a read-back capability, inputs, expect: [{path, op: equals|contains|exists, "
    "value: {const|from}}], level: V1}] or an honest unverifiable_reason."
)


# Message ids as each platform's gateway reports them: Discord snowflakes, Slack message timestamps.
MESSAGE_ID = {"discord": "^[0-9]{17,20}$", "slack": "^[0-9]{9,11}\\.[0-9]{6}$"}


def bug_report(*, repo: str, slack_channel: str, discord_channel: str, reporters: list[str],
               reply_when_closed: bool = True, poll_interval_s: int = 30, wait_days: int = 14,
               intake: str = "discord") -> dict[str, Any]:
    """Bug report in a chat channel -> GitHub issue (unless one already mentions the message) -> Slack notice,
    and optionally: acknowledge, wait for the issue to be closed, and tell the reporter how it was closed.

    ``intake`` is where reports arrive: "discord" (``discord_channel`` is a Discord channel) or "slack" (it is a
    Slack channel, and replies go into the report's thread)."""
    platform = intake.title()
    draft: dict[str, Any] = {
        "intent": f"Turn bug reports posted in {platform} into GitHub issues and tell engineering in Slack",
        "trigger": {
            "kind": "event", "source": f"{intake}:channel:{discord_channel}", "senders": reporters,
            "payload_schema": {"type": "object", "additionalProperties": False,
                               "required": ["message_id", "author", "text"],
                               "properties": {"message_id": {"type": "string", "pattern": MESSAGE_ID[intake]},
                                              "author": {"type": "string"},
                                              "text": {"type": "string", "maxLength": 4000}}},
            "dedupe_key": "message_id", "max_runs_per_hour": 20,
        },
        "steps": [
            {"id": "find", "capability": "capalis:github.issue.search",
             "inputs": {"repo": {"const": repo}, "query": {"from": "trigger.message_id"}}},
            {"id": "create", "capability": "capalis:github.issue.create",
             "when": {"op": "equals", "ref": "steps.find.total_count", "value": 0},
             "inputs": {"repo": {"const": repo}, "title": {"template": "Bug report from {trigger.author}"},
                        "body": {"template": f"{{trigger.text}}\n\n{platform} message {{trigger.message_id}}"}},
             "verify": [{"id": "issue_readback", "probe": "capalis:github.issue.get",
                         "inputs": {"repo": {"const": repo}, "number": {"from": "steps.create.number"}},
                         "expect": [{"path": "state", "op": "equals", "value": {"const": "open"}},
                                    {"path": "body", "op": "contains", "value": {"from": "trigger.message_id"}}],
                         "level": "V1", "deadline_s": 120}]},
            {"id": "notify", "capability": "capalis:slack.message.post", "after": ["create"],
             "inputs": {"channel": {"const": slack_channel},
                        "text": {"template": "New bug report from {trigger.author}: "
                                             "{steps.create.html_url}{steps.find.items.0.html_url}"}},
             "verify": [{"id": "message_readback", "probe": "capalis:slack.message.get",
                         "inputs": {"channel": {"const": slack_channel}, "ts": {"from": "steps.notify.ts"}},
                         "expect": [{"path": "ts", "op": "equals", "value": {"from": "steps.notify.ts"}},
                                    {"path": "text", "op": "contains", "value": {"const": "New bug report"}}],
                         "level": "V1"}]},
        ],
        "budgets": {"max_writes_per_run": 4, "max_runs_per_day": 100},
    }
    if reply_when_closed:
        draft["intent"] += f", and keep the reporter informed in {platform}"
        draft["budgets"]["max_writes_per_run"] = 5
        channel = {"const": discord_channel}
        original = {"from": "trigger.message_id"}

        def reply(step_id: str, after: str, text: str, marker: str, when: dict[str, Any] | None = None
                  ) -> dict[str, Any]:
            if intake == "slack":
                post, probe, id_field = "capalis:slack.message.post", "capalis:slack.message.get", "ts"
                inputs = {"channel": channel, "thread_ts": original, "text": {"template": text}}
                probe_inputs = {"channel": channel, "thread_ts": original, "ts": {"from": f"steps.{step_id}.ts"}}
            else:
                post, probe, id_field = "capalis:discord.message.post", "capalis:discord.message.get", "id"
                inputs = {"channel": channel, "reply_to": original, "text": {"template": text}}
                probe_inputs = {"channel": channel, "id": {"from": f"steps.{step_id}.id"}}
            step: dict[str, Any] = {
                "id": step_id, "capability": post, "after": [after], "inputs": inputs,
                "verify": [{"id": f"{step_id}_readback", "probe": probe, "inputs": probe_inputs,
                            "expect": [{"path": id_field, "op": "equals",
                                        "value": {"from": f"steps.{step_id}.{id_field}"}},
                                       {"path": "text", "op": "contains", "value": {"const": marker}}],
                            "level": "V1"}]}
            if when is not None:
                step["when"] = when
            return step

        draft["steps"] += [
            reply("ack", "notify", "Thanks for the report. It is tracked as "
                  "{steps.create.html_url}{steps.find.items.0.html_url}, and I will reply here when it is closed.",
                  "It is tracked as"),
            {"id": "fixed", "kind": "wait", "capability": "capalis:github.issue.get", "after": ["ack"],
             "inputs": {"repo": {"const": repo},
                        "number": {"first_of": ["steps.create.number", "steps.find.items.0.number"]}},
             "wait": {"until": {"op": "equals", "ref": "steps.fixed.state", "value": "closed"},
                      "poll_interval_s": poll_interval_s, "deadline_s": wait_days * 86400}},
            reply("reply_fixed", "fixed", "Good news: {steps.fixed.html_url} has been fixed and closed. Thanks for "
                  "reporting it.", "has been fixed",
                  {"op": "equals", "ref": "steps.fixed.state_reason", "value": "completed"}),
            reply("reply_closed", "fixed", "Update: {steps.fixed.html_url} was closed without a fix (reason: "
                  "{steps.fixed.state_reason}). Reply here if it still happens.", "closed without a fix",
                  {"op": "not_equals", "ref": "steps.fixed.state_reason", "value": "completed"}),
        ]
    return draft


def site_watch(*, origin: str, path: str, slack_channel: str, every_minutes: int = 10) -> dict[str, Any]:
    """Every few minutes, fetch a page; when it is not up (any status but 200), say so in Slack."""
    if not 1 <= every_minutes <= 60 or 60 % every_minutes:
        raise ValueError("every_minutes must divide an hour (1, 2, 5, 10, 15, 20, 30 or 60)")
    channel = {"const": slack_channel}
    return {
        "intent": f"Check {origin}{path} every {every_minutes} minutes and tell Slack when it is not up",
        "trigger": {"kind": "schedule", "schedule": f"*/{every_minutes} * * * *" if every_minutes < 60 else "0 * * * *",
                    "max_runs_per_hour": 60 // every_minutes},
        "steps": [
            {"id": "check", "capability": "capalis:http.get", "retry": {"max_attempts": 2, "backoff_s": 5},
             "inputs": {"origin": {"const": origin}, "path": {"const": path}}},
            {"id": "alert", "capability": "capalis:slack.message.post",
             "when": {"op": "not_equals", "ref": "steps.check.status", "value": 200},
             "inputs": {"channel": channel,
                        "text": {"template": f"{origin}{path} is not up: it answered HTTP {{steps.check.status}}. "
                                             "Checked by Capalis."}},
             "verify": [{"id": "alert_readback", "probe": "capalis:slack.message.get",
                         "inputs": {"channel": channel, "ts": {"from": "steps.alert.ts"}},
                         "expect": [{"path": "ts", "op": "equals", "value": {"from": "steps.alert.ts"}},
                                    {"path": "text", "op": "contains", "value": {"const": "is not up"}}],
                         "level": "V1"}]},
        ],
        "budgets": {"max_writes_per_run": 1, "max_runs_per_day": min(1000, 24 * 60 // every_minutes)},
    }
