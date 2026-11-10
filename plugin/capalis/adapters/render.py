"""Plain-text views for /capalis. The tools return JSON to the model; people get these."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

_EFFECT_WORD = {"read": "read", "external_write": "write", "external_send": "send", "local_write": "local write",
                "spend": "spend", "delete_or_revoke": "delete", "unknown": "unknown"}


def _when(epoch: int | None) -> str:
    return "-" if not epoch else time.strftime("%H:%M UTC", time.gmtime(epoch))


def _table(rows: Sequence[Sequence[str]]) -> list[str]:
    if not rows:
        return []
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    return ["  " + "  ".join(cell.ljust(w) for cell, w in zip(row[:-1], widths, strict=False)) + "  " + row[-1]
            for row in rows]


_RECIPE_WORDS = {"live": "live", "paused": "paused", "awaiting_approval": "waiting for your approval",
                 "shadow_qualified": "practised, not sent for approval yet", "quarantined": "stopped itself",
                 "retired": "retired"}
_RUN_WORDS = {"verified": "done, every write checked", "unverified": "done, but not every write could be checked",
              "failed": "did not finish", "waiting": "waiting", "running": "running", "queued": "starting",
              "cancelled": "cancelled", "needs_reconciliation": "needs you"}
_STEP_WORDS = {"unknown_effect": "may or may not have been written", "dispatched": "sent, checking it landed",
               "intent_recorded": "about to be sent"}
_PLATFORMS = {"github": "GitHub", "slack": "Slack", "discord": "Discord", "telegram": "Telegram", "http": "web checks"}


def _clip(text: str, limit: int = 90) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def status(report: Mapping[str, Any]) -> str:
    """Short lines and bullets, so it reads the same on a phone as in a terminal."""
    lines = [f"Capalis {report.get('version', '')}".rstrip()]
    if report["globally_paused"]:
        lines.append("Everything is paused. Send /capalis resume to continue.")
    lines += ["", "Recipes"]
    for r in report["recipes"]:
        state = _RECIPE_WORDS.get(r["state"], r["state"].replace("_", " "))
        lines.append(f"• {r['recipe_id']} v{r['version']}: {state}")
        if r["state"] == "awaiting_approval":
            lines.append(f"  → /capalis approve {r['recipe_id']}")
        elif r["state"] == "quarantined":
            lines.append(f"  Why: {_clip(r.get('reason') or 'something it depends on changed')}")
            lines.append(f"  → ask Hermes to rebuild {r['recipe_id']}, then approve it")
        elif r["state"] == "paused":
            lines.append(f"  → /capalis resume {r['recipe_id']}")
    if not report["recipes"]:
        lines.append("• none yet. Ask Hermes to set one up, or send /capalis help")
    elif any(r["state"] == "live" for r in report["recipes"]):
        example = next(r["recipe_id"] for r in report["recipes"] if r["state"] == "live")
        lines.append(f"  To pause one, send /capalis pause and its name, e.g. /capalis pause {example}")
    lines += ["", "Recent runs"]
    for r in report["recent_runs"][:5]:
        lines += _run_lines(r)
    if not report["recent_runs"]:
        lines.append("• none yet")
    lines += ["", "Needs you"]
    lines += [f"• run {a['run_id'][:8]}, step {a['step']}: "
              f"{_STEP_WORDS.get(a['state'], a['state'].replace('_', ' '))}" for a in report["needs_attention"]]
    if not report["needs_attention"]:
        lines.append("• nothing")
    caps = report["capabilities"]
    missing = sorted({_PLATFORMS.get(cid.split(":", 1)[1].split(".")[0], cid.split(":", 1)[1])
                      for cid, state in caps.items() if state != "available"})
    ready = sum(1 for state in caps.values() if state == "available")
    lines += ["", f"Tools ready: {ready} of {len(caps)}"
              + (f" (needs setup: {', '.join(missing)}. Send /capalis setup)" if missing else "")]
    lines.append("Event log: " + ("intact" if report["event_log_intact"] else "TAMPERING DETECTED"))
    return "\n".join(lines)


def named(value: str | None, names: Mapping[str, str], kind: str | None = None) -> str:
    """'#engineering (slack.channel:C0CFU6F0MNK)' when a display name is known, else the id."""
    if value is None:
        return "-"
    key = f"{kind}:{value}" if kind else value
    if key.count(":") >= 2 and key.split(":")[1] == "channel":
        platform, _, channel = key.partition(":channel:")
        key = f"{platform}.channel:{channel}"
    label = names.get(key)
    return f"{label} ({value})" if label else value


def named_text(text: str, names: Mapping[str, str]) -> str:
    """Put display names next to the ids that appear in a sentence."""
    for key, label in names.items():
        value = key.split(":", 1)[1]
        if value in text and label not in text:
            text = text.replace(value, f"{value} ({label})", 1)
    return text


def recipe(desc: Mapping[str, Any]) -> str:
    trigger, names = desc["trigger"], desc.get("names", {})
    senders = ", ".join(named(s, names, "discord.user") for s in trigger["senders"])
    lines = [f"{desc['recipe_id']} v{desc['version']} - {desc['state'].replace('_', ' ')} - digest "
             f"{desc['short_digest']}", desc["intent"],
             f"Trigger: {trigger['kind']} from {named(trigger['source'], names)}"
             + (f", only from {senders}" if senders else ""), "Steps:"]
    rows = []
    for n, step in enumerate(desc["steps"], start=1):
        checks = ", ".join(f"{c['id']} ({c['level']})" for c in step["checks"])
        note = (f"checked by {checks}" if checks else step["unverifiable_reason"] or "")
        if step.get("wait"):
            note = f"waits until {step['wait']}"
        rows.append([f"{n}. {step['id']}", "wait" if step.get("kind") == "wait" else
                     _EFFECT_WORD.get(step["effect"], step["effect"]),
                     named(step["target"], names), ("if condition holds; " if step["when"] else "") + note])
    lines += _table(rows)
    budgets = desc["budgets"]
    lines += [f"Approval allows: {', '.join(desc['effects'])} on "
              f"{', '.join(named(t, names) for t in desc['targets'])}",
              f"Limits: {budgets['max_writes_per_run']} writes per run, {budgets['max_runs_per_hour']} runs per hour, "
              f"{budgets['max_runs_per_day']} per day"]
    changes = desc.get("changes_since_previous")
    if changes is not None:
        lines.append(f"Changes since v{desc['version'] - 1}:" + ("" if changes else " none"))
        lines += [f"  - {named_text(change, names)}" for change in changes]
    if desc["state"] == "awaiting_approval":
        lines.append(f"To approve this exact version: /capalis approve {desc['recipe_id']} {desc['short_digest']}")
    return "\n".join(lines)


def _run_lines(r: Mapping[str, Any]) -> list[str]:
    practice = r["mode"] == "shadow"
    words = ("practice run, nothing was sent" if practice and r["state"] in ("verified", "unverified")
             else _RUN_WORDS.get(r["state"], r["state"].replace("_", " ")) + (" (practice)" if practice else ""))
    lines = [f"• {_when(r['started_at'])} {r['recipe_id']}: {words} · {r['run_id'][:8]}"]
    if r["state"] in ("failed", "needs_reconciliation", "waiting") and r["detail"]:
        lines.append(f"  {_clip(r['detail'])}")
    return lines


def runs(items: Sequence[Mapping[str, Any]]) -> str:
    if not items:
        return "No runs yet."
    lines = [line for r in items for line in _run_lines(r)]
    return "\n".join([*lines, "", f"Every step of a run: /capalis evidence {items[0]['run_id'][:8]}"])


def _output_summary(output: Any) -> str:
    if not isinstance(output, Mapping):
        return ""
    for key in ("html_url", "ts", "id"):
        if output.get(key):
            return f"{key.replace('html_url', 'url')} {output[key]}"
    if "total_count" in output:
        return f"{output['total_count']} found"
    return ""


def evidence(report: Mapping[str, Any]) -> str:
    lines = [f"Run {report['run_id'][:8]} - {report['recipe_id']} - {report['mode']} - "
             f"{report['state'].replace('_', ' ').upper()}", report["detail"], "Steps:"]
    lines += _table([[s["step"], s["state"].replace("_", " "), f"attempt {s['attempt']}",
                      _output_summary(s["output"]) or s["detail"]] for s in report["steps"]])
    lines.append("Independent checks:")
    lines += _table([[c["check"], c["level"], c["status"], f"{c['probe']} on {c['target']}"
                      + (f" ({c['error']})" if c["error"] else "")] for c in report["independent_checks"]]) or [
        "  none (nothing was written, or the writes could not be checked)"]
    lines.append(f"Event log: {report['events']} events, "
                 + ("intact" if report["event_log_intact"] else f"broken at #{report['event_log_broken_at']}"))
    return "\n".join(lines)


_WRITE_WORDS = {"capalis:github.issue.create": "open an issue in", "capalis:slack.message.post": "post in",
                "capalis:slack.webhook.post": "post (unverifiable) in", "capalis:discord.message.post": "post in"}


_NOUNS = {"github.issue": "the issue", "slack.message": "the Slack message",
          "discord.message": "the Discord message", "http.get": "the page"}


def _label(value: str | None, names: Mapping[str, str], kind: str | None = None) -> str:
    """'#engineering' when a display name is known, else the id; a web origin as its bare host."""
    if value and value.startswith("http.origin:"):
        return value.split("://", 1)[-1]
    full = named(value, names, kind)
    return full.split(" (")[0] if " (" in full else full


_DAYS = {"*": "Every day", "1-5": "Every weekday"}


def _schedule_words(cron: str) -> str:
    """The common shapes in words ('Every 10 minutes'); anything else as the cron line, which is exact."""
    minute, hour, dom, month, dow = (cron.split() + ["?"] * 5)[:5]
    if (dom, month, dow) == ("*", "*", "*") and hour == "*":
        if minute.startswith("*/") and minute[2:].isdigit():
            return f"Every {minute[2:]} minutes" if minute[2:] != "1" else "Every minute"
        if minute.isdigit():
            return "Every hour" if minute == "0" else f"Every hour at :{int(minute):02d}"
    if minute.isdigit() and hour.isdigit() and (dom, month) == ("*", "*") and dow in _DAYS:
        return f"{_DAYS[dow]} at {int(hour):02d}:{int(minute):02d} UTC"
    return f"On the schedule {cron} (UTC)"


def _trigger_text(desc: Mapping[str, Any]) -> str:
    trigger, names = desc["trigger"], desc.get("names", {})
    if trigger["kind"] == "schedule":
        return _schedule_words(str(trigger.get("schedule") or ""))
    platform = str(trigger["source"] or "discord").split(":", 1)[0]
    who = ", ".join(_label(s, names, f"{platform}.user") for s in trigger["senders"]) or "the owner"
    return f"When {who} posts in {_label(trigger['source'], names)}"


def approved(desc: Mapping[str, Any], days: int) -> str:
    names = desc.get("names", {})
    places = sorted({_label(t, names) for t in desc["targets"]})
    return "\n".join([f"✅ Approved {desc['recipe_id']} v{desc['version']} for {days} days.", "",
                      f"{_trigger_text(desc)}, it now runs on its own.",
                      f"It can only act in: {', '.join(places)}.", "",
                      f"Pause it any time: /capalis pause {desc['recipe_id']}"])


def plain(desc: Mapping[str, Any]) -> str:
    """What a non-technical approver checks: who triggers it, then one line per thing it will do, and where."""
    names = desc.get("names", {})

    nouns = {step["id"]: next((noun for prefix, noun in _NOUNS.items()
                               if (step.get("capability") or "").split(":", 1)[-1].startswith(prefix)), step["id"])
             for step in desc["steps"]}

    def condition(text: str) -> str:
        def words(match: re.Match[str]) -> str:
            noun, field = nouns.get(match.group(1), match.group(1)), match.group(2)
            return f"{noun} is" if field == "state" else f"{noun}'s {field.replace('_', ' ')} is"
        return re.sub(r"\b(\w+)\.(\w+) is", words, text.replace("steps.", "").replace(" equals ", " is "))

    planned = []
    for step in desc["steps"]:
        if step.get("kind") == "wait":
            planned.append((step, f"Wait until {condition(step['wait'].split(', checking')[0])}"))
        elif step.get("url"):
            planned.append((step, f"Check {step['url'].split('://', 1)[-1]}"))
        elif step["effect"] != "read":
            verb = _WRITE_WORDS.get(step["capability"], f"{step['effect'].replace('_', ' ')} on")
            planned.append((step, f"{verb[0].upper()}{verb[1:]} {_label(step['target'], names)}"))
    repeated = {text for _, text in planned if sum(1 for _, other in planned if other == text) > 1}
    actions = [text + (f": {step['id'].replace('_', ' ')}" if text in repeated else "")
               + (" (only if needed)" if step["when"] else "") for step, text in planned]
    lines = [f"{_trigger_text(desc)}, it will:", *(f"• {action}" for action in actions), "",
             "Every write is read back before it counts as done."]
    changes = desc.get("changes_since_previous")
    if changes:
        lines += ["", f"Changed since v{desc['version'] - 1}:",
                  *(f"• {named_text(change, names)}" for change in changes[:3])]
        lines += [f"• and {len(changes) - 3} more (/capalis show {desc['recipe_id']})"] if len(changes) > 3 else []
    return "\n".join(lines)


def setup(checks: Sequence[Mapping[str, Any]]) -> str:
    mark = {True: "OK  ", False: "FIX ", None: "NOTE"}
    lines = ["Capalis setup"]
    for c in checks:
        lines.append(f"  {mark[c['ok']]} {c['area']}: {c['detail']}")
        if c["fix"] and c["ok"] is not True:
            lines.append(f"       -> {c['fix']}")
    missing = sum(1 for c in checks if c["ok"] is False)
    lines.append("Everything needed is ready." if not missing else f"{missing} thing(s) to fix.")
    return "\n".join(lines)
