"""Plain-text views for /capalis. The tools return JSON to the model; people get these."""

from __future__ import annotations

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


def status(report: Mapping[str, Any]) -> str:
    lines = [f"Capalis {report.get('version', '')}" + ("  (ALL PAUSED)" if report["globally_paused"] else "")]
    lines.append("Recipes:")
    lines += _table([[r["recipe_id"], f"v{r['version']}", r["state"].replace("_", " "), r["short_digest"]]
                     for r in report["recipes"]]) or ["  none yet"]
    for q in report.get("quarantined", []):
        lines.append(f"  quarantined {q['recipe_id']} v{q['version']}: {q['reason']}")
    lines.append("Recent runs:")
    lines += _table([[r["run_id"][:8], r["mode"], r["state"].replace("_", " "), _when(r["started_at"]),
                      r["detail"]] for r in report["recent_runs"]]) or ["  none yet"]
    attention = report["needs_attention"]
    lines.append("Needs your attention: " + ("nothing" if not attention else ""))
    lines += [f"  run {a['run_id'][:8]} step {a['step']}: {a['state'].replace('_', ' ')} ({a['detail']})"
              for a in attention]
    ready = [cid.split(":", 1)[1] for cid, state in report["capabilities"].items() if state == "available"]
    missing = [cid.split(":", 1)[1] for cid, state in report["capabilities"].items() if state != "available"]
    lines.append(f"Capabilities ready: {len(ready)}" + (f"; needs setup: {', '.join(missing)}" if missing else ""))
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


def runs(items: Sequence[Mapping[str, Any]]) -> str:
    if not items:
        return "No runs yet."
    return "\n".join(_table([[r["run_id"][:8], r["recipe_id"], r["mode"], r["state"].replace("_", " "),
                              _when(r["started_at"]), r["detail"]] for r in items]))


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


def plain(desc: Mapping[str, Any]) -> str:
    """One paragraph a non-technical approver can check: who triggers it, and what it will do where."""
    names = desc.get("names", {})

    def short(value: str | None, kind: str | None = None) -> str:
        full = named(value, names, kind)
        return full.split(" (")[0] if " (" in full else full

    trigger = desc["trigger"]
    who = ", ".join(short(s, "discord.user") for s in trigger["senders"]) or "the owner"
    actions = []
    for step in desc["steps"]:
        if step.get("kind") == "wait":
            actions.append(f"wait until {step['wait'].split(', checking')[0].replace('steps.', '')}")
        elif step["effect"] != "read":
            verb = _WRITE_WORDS.get(step["capability"], f"{step['effect'].replace('_', ' ')} on")
            actions.append(f"{verb} {short(step['target'])}" + (" (if needed)" if step["when"] else ""))
    when = (f"On the schedule {trigger.get('schedule')}" if trigger["kind"] == "schedule"
            else f"When {who} posts in {short(trigger['source'])}")
    text = (f"{when}, it will " + ", then ".join(actions)
            + ". Every write is read back independently before it counts as done.")
    changes = desc.get("changes_since_previous")
    if changes:
        shown = "; ".join(named_text(c, names) for c in changes[:4]) + ("; ..." if len(changes) > 4 else "")
        text += f" Changed since v{desc['version'] - 1}: {shown}."
    return text


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
