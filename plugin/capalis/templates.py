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


def bug_report(*, repo: str, slack_channel: str, discord_channel: str, reporters: list[str],
               reply_when_closed: bool = True, poll_interval_s: int = 30,
               wait_days: int = 14) -> dict[str, Any]:
    """Discord bug report -> GitHub issue (unless one already mentions the message) -> Slack notice, and
    optionally: wait for the issue to be closed -> reply to the reporter in Discord."""
    draft: dict[str, Any] = {
        "intent": "Turn bug reports posted in Discord into GitHub issues and tell engineering in Slack",
        "trigger": {
            "kind": "event", "source": f"discord:channel:{discord_channel}", "senders": reporters,
            "payload_schema": {"type": "object", "additionalProperties": False,
                               "required": ["message_id", "author", "text"],
                               "properties": {"message_id": {"type": "string", "pattern": "^[0-9]{17,20}$"},
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
                        "body": {"template": "{trigger.text}\n\nDiscord message {trigger.message_id}"}},
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
        draft["intent"] += ", and keep the reporter informed in Discord"
        draft["budgets"]["max_writes_per_run"] = 5
        channel = {"const": discord_channel}
        original = {"from": "trigger.message_id"}

        def reply(step_id: str, after: str, text: str, marker: str, when: dict[str, Any] | None = None
                  ) -> dict[str, Any]:
            step: dict[str, Any] = {
                "id": step_id, "capability": "capalis:discord.message.post", "after": [after],
                "inputs": {"channel": channel, "reply_to": original, "text": {"template": text}},
                "verify": [{"id": f"{step_id}_readback", "probe": "capalis:discord.message.get",
                            "inputs": {"channel": channel, "id": {"from": f"steps.{step_id}.id"}},
                            "expect": [{"path": "id", "op": "equals", "value": {"from": f"steps.{step_id}.id"}},
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
