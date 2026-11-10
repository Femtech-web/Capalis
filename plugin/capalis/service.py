"""Capalis application service: what the chat tools, slash commands and gateway hook call.

It is independent of Hermes so it can be tested directly. Approval, resume, confirmation and manual
resolution are only reachable through ``Actor``-checked methods that the slash commands call; the
model-facing tools never expose them (I-02).
"""

from __future__ import annotations

import contextlib
import functools
import json
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .core.compiler import compile_recipe
from .core.drift import detect_drift
from .core.expressions import condition_to_wire
from .core.lifecycle import IllegalTransition, RecipeState, RunState, StepState
from .core.matching import ObservedStats, Requirement, match_requirement
from .core.model import Effect, EvidenceLevel, RecipeSpec, Target, TriggerKind, grant_for
from .core.policy import Mode
from .runtime.ports import Invocation, InvokeResult, Outcome
from .runtime.runner import Runner, Submission
from .storage.db import Store, StoreError

RECIPE_ID = re.compile(r"[a-z][a-z0-9-]{2,39}")
LOCAL_SURFACES = frozenset({"cli", "tui", "desktop", "local"})


class ServiceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Actor:
    """Who typed a slash command, from the gateway's transport identity (never from display names)."""

    platform: str
    user_id: str | None

    @property
    def id(self) -> str:
        return self.platform if self.platform in LOCAL_SURFACES else f"{self.platform}:{self.user_id}"


class Capalis:
    def __init__(self, store: Store, adapters: Mapping[str, Any], *, workspace: str = "default",
                 approvers: Callable[[], Sequence[str]] = lambda: (), runner: Runner | None = None,
                 directory: Any = None, alert_channel: Callable[[], str | None] = lambda: None,
                 export_dir: Callable[[], Any] | None = None,
                 dispatch: Callable[[str, dict[str, Any]], Any] | None = None,
                 has_plugin: Callable[[str], bool] | None = None) -> None:
        self.alert_channel = alert_channel
        self.export_dir = export_dir
        self.dispatch = dispatch
        self.has_plugin = has_plugin
        self.store = store
        self.adapters = adapters
        self.directory = directory
        self.workspace = workspace
        self.approvers = approvers
        self.runner = runner or Runner(store, adapters)
        self._work: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._stopping = threading.Event()
        self._load_contracts()

    def descriptors(self) -> dict[str, Any]:
        return {cid: adapter.descriptor for cid, adapter in self.adapters.items()}

    # -- Planning (model-facing) --------------------------------------------------------------------

    def capabilities(self) -> list[dict[str, Any]]:
        return [{"id": cap.id, "summary": cap.summary, "effect": cap.effect.value, "operation": cap.operation,
                 "target": None if cap.target is None else {"param": cap.target.param, "kind": cap.target.kind},
                 "availability": cap.availability.value, "idempotency": cap.idempotency.value,
                 "readback": list(cap.readback), "limits": list(cap.limits),
                 "input_schema": cap.input_schema, "output_schema": cap.output_schema, "setup": cap.setup_hint}
                for cap in self.descriptors().values()]

    def observed(self) -> dict[str, ObservedStats]:
        """What each capability actually did in this install's live runs (FR-MEM-03): counts, never rates."""
        counts: dict[str, dict[str, int]] = {}
        recipes: dict[str, RecipeSpec] = {}
        for run in self.store.runs(limit=500):
            if run.mode is not Mode.LIVE:
                continue
            recipe = recipes.get(run.recipe_digest) or recipes.setdefault(run.recipe_digest,
                                                                          self.store.recipe(run.recipe_digest))
            for attempt in self.store.latest_attempts(run.run_id).values():
                step = recipe.step(attempt.step_id)
                if step is None or not step.is_side_effect:
                    continue
                bucket = counts.setdefault(step.capability_id, {"verified": 0, "failed": 0, "unverified": 0})
                if attempt.state is StepState.OBSERVED_VERIFIED:
                    bucket["verified"] += 1
                elif attempt.state is StepState.FAILED:
                    bucket["failed"] += 1
                elif attempt.state is StepState.UNVERIFIED:
                    bucket["unverified"] += 1
        return {cid: ObservedStats(environment="this install's live runs", **c) for cid, c in counts.items()}

    def compare(self, needs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        registry = self.descriptors()
        results = []
        for raw in needs:
            try:
                requirement = Requirement(
                    need_id=str(raw["need_id"]), operation=str(raw["operation"]),
                    description=str(raw.get("description", "")), max_effect=Effect(raw.get("max_effect", "read")),
                    min_evidence=None if raw.get("min_evidence") == "none"
                    else EvidenceLevel(raw.get("min_evidence", "V1")))
            except (KeyError, ValueError) as exc:
                raise ServiceError(f"each need takes need_id, operation, max_effect and min_evidence: {exc}") from exc
            match = match_requirement(requirement, registry, self.observed())
            results.append({
                "need_id": requirement.need_id, "recommended": match.recommended, "rationale": match.rationale,
                "candidates": [{"id": a.capability_id, "can_run": a.executable, "excluded_because": list(a.exclusions),
                                "factors": {f.name: {"summary": f.summary, "basis": f.basis.value} for f in a.factors},
                                "setup": a.setup}
                               for a in match.candidates],
            })
        return results

    def bug_report_template(self, *, repo: str, slack_channel: str, discord_channel: str, reporters: Sequence[str],
                            reply_when_closed: bool = True, intake: str = "discord") -> dict[str, Any]:
        """The bug-report draft, with names people use ("#bugs", "capalis-sandbox", "@ada") resolved to ids."""
        from .templates import DRAFT_FORMAT, bug_report

        resolved: list[dict[str, str]] = []
        labels: dict[str, str] = {}

        def resolve(kind: str, raw: str, lookup: Callable[[], Any]) -> str:
            if self.directory is None:
                return raw
            try:
                hit = lookup()
            except ValueError as exc:
                raise ServiceError(str(exc)) from exc
            labels[f"{kind}:{hit.value}"] = hit.label
            resolved.append({"asked": raw, "kind": kind, "id": hit.value, "name": hit.label, "note": hit.note})
            return str(hit.value)

        d = self.directory
        if intake not in ("discord", "slack"):
            raise ServiceError("intake is discord or slack")
        repo_id = resolve("github.repo", repo, lambda: d.github_repo(repo))
        slack_id = resolve("slack.channel", slack_channel, lambda: d.slack_channel(slack_channel))
        if intake == "discord":
            report_id = resolve("discord.channel", discord_channel, lambda: d.discord_channel(discord_channel))
            people = [resolve("discord.user", who, functools.partial(d.discord_user, who, report_id)) if d else who
                      for who in reporters]
        else:
            report_id = resolve("slack.channel", discord_channel, lambda: d.slack_channel(discord_channel))
            people = [resolve("slack.user", who, functools.partial(d.slack_user, who)) if d else who
                      for who in reporters]
        self.store.set_names(labels)
        draft = bug_report(repo=repo_id, slack_channel=slack_id, discord_channel=report_id, reporters=people,
                           reply_when_closed=reply_when_closed, intake=intake)
        return {"draft": draft, "resolved": resolved, "format": DRAFT_FORMAT,
                "notes": [f"{r['name']}: {r['note']}" for r in resolved if r["note"]]}

    def site_watch_template(self, *, url: str, slack_channel: str, every_minutes: int = 10) -> dict[str, Any]:
        from urllib.parse import urlsplit

        from .templates import DRAFT_FORMAT, site_watch

        parts = urlsplit(url.strip())
        if parts.scheme != "https" or not parts.netloc:
            raise ServiceError("give the full https:// address to watch")
        origin, path = f"https://{parts.netloc}", parts.path or "/"
        channel = slack_channel
        resolved = []
        if self.directory is not None:
            try:
                hit = self.directory.slack_channel(slack_channel)
            except ValueError as exc:
                raise ServiceError(str(exc)) from exc
            channel = hit.value
            self.store.set_names({f"slack.channel:{hit.value}": hit.label})
            resolved.append({"asked": slack_channel, "kind": "slack.channel", "id": hit.value, "name": hit.label,
                             "note": hit.note})
        return {"draft": site_watch(origin=origin, path=path, slack_channel=channel, every_minutes=every_minutes),
                "resolved": resolved, "format": DRAFT_FORMAT}

    def compile(self, recipe_id: str, draft: Mapping[str, Any], *, owner: str) -> dict[str, Any]:
        if not RECIPE_ID.fullmatch(recipe_id):
            raise ServiceError("recipe_id must be 3-40 lowercase letters, digits or dashes, starting with a letter")
        latest = self.store.latest(recipe_id)
        registry = self.descriptors()
        if latest is not None:
            same = compile_recipe(dict(draft), registry, recipe_id=recipe_id, version=latest[0].version, owner=owner,
                                  workspace=self.workspace, previous_digest=latest[0].previous_digest)
            if same.ok and same.recipe is not None and same.recipe.digest == latest[0].digest:
                return {"ok": True, "problems": [], "setup_needed": [], "unchanged": True,
                        **self.describe(latest[0])}
        version = 1 if latest is None else latest[0].version + 1
        result = compile_recipe(dict(draft), registry, recipe_id=recipe_id, version=version, owner=owner,
                                workspace=self.workspace, previous_digest=None if latest is None else latest[0].digest)
        report: dict[str, Any] = {
            "ok": result.ok,
            "problems": [{"code": p.code, "path": p.path, "message": p.message} for p in result.problems],
            "setup_needed": [{"capability": s.capability_id, "availability": s.availability.value,
                              "credential": s.auth_requirement} for s in result.setup_needed],
        }
        if result.recipe is not None and result.ok:
            self.store.save_recipe(result.recipe)
            report.update(self.describe(result.recipe))
        return report

    def qualify(self, recipe_id: str, sample_payload: Mapping[str, Any]) -> dict[str, Any]:
        """Shadow-run the latest version on a sample trigger: real reads, simulated writes, nothing sent."""
        recipe, state = self._latest(recipe_id)
        if state not in (RecipeState.COMPILED, RecipeState.TESTED, RecipeState.SHADOW_QUALIFIED):
            raise ServiceError(f"version {recipe.version} is {state.value}; only a newly compiled version is qualified")
        sender = next(iter(sorted(recipe.trigger.senders)), "")
        sub = self.runner.submit(recipe, source=recipe.trigger.source, sender=sender, payload=sample_payload,
                                 mode=Mode.SHADOW, dedupe_value=f"qualify-{uuid.uuid4().hex[:12]}")
        report = {"accepted": sub.accepted, "code": sub.code, "detail": sub.detail}
        if sub.run_id is not None:
            report["run"] = self.evidence(sub.run_id)
        passed = (sub.accepted and sub.run_id is not None and sub.state is RunState.UNVERIFIED
                  and self.store.run(sub.run_id).detail.startswith("shadow run:"))
        if passed:
            for target in (RecipeState.TESTED, RecipeState.SHADOW_QUALIFIED, RecipeState.AWAITING_APPROVAL):
                if self.store.recipe_state(recipe.digest) is not target:
                    self.store.transition(recipe.digest, target, reason="shadow run passed")
        report["qualified"] = passed
        report["state"] = self.store.recipe_state(recipe.digest).value
        report["next"] = (f"Ask the owner to type /capalis approve {recipe_id} to review a plain summary and approve "
                          f"with a button (or /capalis approve {recipe_id} {recipe.digest[7:19]} where buttons are "
                          "unavailable)"
                          if passed else "Fix the problems above and compile a new version.")
        return report

    def run_manual(self, recipe_id: str, payload: Mapping[str, Any], *, sender: str) -> dict[str, Any]:
        recipe, _ = self._latest(recipe_id)
        if recipe.trigger.kind is not TriggerKind.MANUAL:
            raise ServiceError("this Recipe runs on its trigger, not on request")
        sub = self.runner.submit(recipe, source=recipe.trigger.source, sender=sender, payload=payload,
                                 dedupe_value=f"manual-{uuid.uuid4().hex[:12]}")
        return self._submission(sub)

    def pause(self, recipe_id: str | None, *, actor: str, reason: str = "") -> str:
        """Pausing only ever reduces what can happen, so the model may do it as well as the owner."""
        if recipe_id is None:
            self.store.set_global_pause(True, actor=actor, reason=reason)
            return "All Capalis Recipes are paused. Runs in progress hold before their next write."
        recipe = self.store.version_in_state(recipe_id, RecipeState.LIVE)
        if recipe is None:
            return f"{recipe_id} has no live version; nothing to pause."
        self.store.transition(recipe.digest, RecipeState.PAUSED, reason=reason or f"paused by {actor}")
        return f"{recipe_id} v{recipe.version} is paused. To start it again: /capalis resume {recipe_id}"

    # -- Owner-only (slash commands) ----------------------------------------------------------------

    def authorize_actor(self, actor: Actor) -> None:
        if actor.platform in LOCAL_SURFACES:
            return
        if actor.user_id is None or actor.id not in set(self.approvers()):
            raise ServiceError(f"{actor.id} is not an approver; add it to the Capalis approvers setting")

    def approve(self, recipe_id: str, digest_prefix: str, actor: Actor) -> str:
        self.authorize_actor(actor)
        recipe, state = self._latest(recipe_id)
        if state is not RecipeState.AWAITING_APPROVAL:
            raise ServiceError(f"{recipe_id} v{recipe.version} is {state.value}; only a shadow-qualified version "
                               "awaiting approval can be approved")
        if len(digest_prefix) < 8 or not recipe.digest[7:].startswith(digest_prefix):
            raise ServiceError(f"that does not match version {recipe.version} ({recipe.digest[7:19]}); "
                               "review the current version before approving")
        grant = grant_for(recipe, grant_id=uuid.uuid4().hex, actor=actor.id, now=self.store.clock())
        self.store.save_grant(grant)
        for state in (RecipeState.LIVE, RecipeState.PAUSED):
            previous = self.store.version_in_state(recipe_id, state)
            if previous is not None and previous.digest != recipe.digest:
                self.store.transition(previous.digest, RecipeState.RETIRED,
                                      reason=f"superseded by v{recipe.version}")
        self.store.transition(recipe.digest, RecipeState.LIVE, reason=f"approved by {actor.id}")
        from .adapters.render import approved

        return approved(self.describe(recipe), (grant.expires_at - grant.issued_at) // 86400)

    def resume(self, recipe_id: str | None, actor: Actor) -> str:
        self.authorize_actor(actor)
        if recipe_id is None:
            self.store.set_global_pause(False, actor=actor.id)
            held = [r.run_id for r in self.store.runs() if r.state is RunState.WAITING and r.resume_at is None]
            for run_id in held:
                self._enqueue(run_id)
            return f"Capalis is running again; {len(held)} held run(s) will continue."
        recipe = self.store.version_in_state(recipe_id, RecipeState.PAUSED)
        if recipe is None:
            raise ServiceError(f"{recipe_id} has no paused version")
        drift = detect_drift(recipe, self.descriptors())
        if drift.blocking:
            self.store.transition(recipe.digest, RecipeState.QUARANTINED, reason=drift.plain(),
                                  detail=drift.summary())
            raise ServiceError(f"something it depends on changed while paused, so it is quarantined: "
                               f"{drift.plain()}")
        self.store.transition(recipe.digest, RecipeState.LIVE, reason=f"resumed by {actor.id}")
        return f"{recipe_id} v{recipe.version} is live again."

    def revoke(self, recipe_id: str, actor: Actor) -> str:
        self.authorize_actor(actor)
        recipe = (self.store.version_in_state(recipe_id, RecipeState.LIVE)
                  or self.store.version_in_state(recipe_id, RecipeState.PAUSED))
        if recipe is None:
            raise ServiceError(f"{recipe_id} has no approved version")
        grant = self.store.active_grant(recipe.digest)
        if grant is not None:
            self.store.revoke_grant(grant.grant_id, actor=actor.id)
        self.store.transition(recipe.digest, RecipeState.AWAITING_APPROVAL, reason=f"revoked by {actor.id}")
        return f"Approval for {recipe_id} v{recipe.version} revoked."

    def cancel(self, run_id: str, actor: Actor) -> dict[str, Any]:
        self.authorize_actor(actor)
        run_id = self.find_run(run_id)
        self.runner.cancel(run_id, actor=actor.id)
        return self.evidence(run_id)

    def prefer(self, actor: Actor, presentation: str) -> str:
        """FR-MAT-05: how comparisons are presented; it never changes what needs approval."""
        self.store.set_presentation(actor.id, presentation)
        words = {"recommend": "lead with the recommended option", "compare": "show the full comparison first",
                 "ask": "ask each time"}
        return f"Capalis will {words[presentation]} for {actor.id}. Approval is still required either way."

    def confirm(self, run_id: str, step_id: str, actor: Actor) -> dict[str, Any]:
        self.authorize_actor(actor)
        run_id = self.find_run(run_id)
        self.runner.confirm(run_id, step_id, actor=actor.id)
        return self.evidence(run_id)

    def resolve(self, run_id: str, step_id: str, applied: bool, actor: Actor) -> dict[str, Any]:
        self.authorize_actor(actor)
        run_id = self.find_run(run_id)
        self.runner.resolve_unknown(run_id, step_id, applied=applied, actor=actor.id)
        return self.evidence(run_id)

    def setup_report(self) -> list[dict[str, Any]]:
        """What is connected, what each Recipe can reach, and the exact fix for anything missing."""
        if self.directory is None:
            raise ServiceError("setup checks need the Hermes runtime")
        from .adapters.directory import readiness

        active = [self.store.recipe(digest) for _, _, digest, state in self.store.recipes()
                  if state not in (RecipeState.RETIRED, RecipeState.QUARANTINED)]
        targets = {str(t) for recipe in active for t in recipe.targets}
        discord = {r.trigger.source.rsplit(":", 1)[1] for r in active if r.trigger.source.startswith("discord:")}
        slack = {r.trigger.source.rsplit(":", 1)[1] for r in active if r.trigger.source.startswith("slack:")}
        reporters = {platform: {s for r in active if r.trigger.source.startswith(f"{platform}:")
                                for s in r.trigger.senders} for platform in ("slack", "discord")}
        return [{"area": c.area, "ok": c.ok, "detail": c.detail, "fix": c.fix}
                for c in readiness(self.directory, targets, discord, slack, reporters)]

    def drift_sweep(self) -> list[dict[str, str]]:
        """Quarantine approved versions whose pinned capabilities changed, before any trigger arrives."""
        registry = self.descriptors()
        quarantined = []
        for recipe_id in sorted({row[0] for row in self.store.recipes()}):
            for state in (RecipeState.LIVE, RecipeState.PAUSED):
                recipe = self.store.version_in_state(recipe_id, state)
                if recipe is None:
                    continue
                report = detect_drift(recipe, registry)
                if report.blocking:
                    self.store.transition(recipe.digest, RecipeState.QUARANTINED, reason=report.plain(),
                                          detail=report.summary())
                    quarantined.append({"recipe_id": recipe_id, "version": str(recipe.version),
                                        "reason": report.plain()})
        return quarantined

    # -- Schedules ----------------------------------------------------------------------------------

    def fire_schedules(self, now: int | None = None) -> list[str]:
        """Start runs for live schedule Recipes whose time has come (FR-EXE-06). The dedupe value is the slot,
        so a slot fires once even if several workers or restarts look at it."""
        from .core.cron import Cron, iso_minute

        at = self.store.clock() if now is None else now
        fired: list[str] = []
        for recipe_id in sorted({row[0] for row in self.store.recipes()}):
            recipe = self.store.version_in_state(recipe_id, RecipeState.LIVE)
            if recipe is None or recipe.trigger.kind is not TriggerKind.SCHEDULE or not recipe.trigger.schedule:
                continue
            key = f"schedule:{recipe.digest}"
            last = self.store.control(key)
            self.store.set_control(key, str(at))
            if last is None:
                continue  # start counting from approval, never backfill
            slot = Cron.parse(recipe.trigger.schedule).latest_between(int(last), at)
            if slot is None:
                continue
            sub = self.runner.submit(recipe, source=recipe.trigger.source, sender=sorted(recipe.trigger.senders)[0],
                                     payload={}, dedupe_value=f"schedule-{iso_minute(slot)}", advance=False)
            if sub.run_id is not None and sub.code == "queued":
                self._enqueue(sub.run_id)
                fired.append(sub.run_id)
        return fired

    # -- Owner alerts -------------------------------------------------------------------------------

    def send_alerts(self, limit: int = 10) -> int:
        """Tell the owner, in the channel they chose, about anything that now needs a person.

        Driven by the event log, so an alert survives restarts and is sent once: the cursor only moves past
        an event after its alert was delivered (or when the event needs none).
        """
        spec = (self.alert_channel() or "").strip()
        if not spec or self.directory is None:
            return 0
        if not self.store.claim_control("alerts_lease", ttl_s=300):
            return 0
        try:
            return self._send_alerts(spec, limit)
        finally:
            self.store.release_control("alerts_lease")

    def _send_alerts(self, spec: str, limit: int) -> int:
        cursor = self.store.control("alerts_seq")
        if cursor is None:
            self.store.set_control("alerts_seq", str(self.store.last_event_seq()))
            return 0
        platform, _, name = spec.partition(":")
        cid = {"slack": "capalis:slack.message.post", "discord": "capalis:discord.message.post",
               "telegram": "capalis:telegram.message.post"}.get(platform)
        if cid is None or cid not in self.adapters:
            return 0
        try:
            channel = name.strip() if platform == "telegram" else (
                self.directory.slack_channel(name) if platform == "slack" else self.directory.discord_channel(name)
            ).value
        except ValueError:
            return 0
        sent = 0
        for event in self.store.events_after(int(cursor)):
            text = self._alert_text(event, platform)
            if text is not None:
                if sent >= limit:
                    break
                field, kind = ("chat", "telegram.chat") if platform == "telegram" else ("channel",
                                                                                     f"{platform}.channel")
                call = Invocation(cid, Target(kind, channel), {field: channel, "text": text},
                                  f"cap-alert-{event.seq}", 20)
                result = self.adapters[cid].invoke(call)
                if result.outcome is not Outcome.OK:
                    self.store.append_event("alert.failed", {"seq": event.seq, "error": result.error or ""})
                    break
                self.store.append_event("alert.sent", {"seq": event.seq, "channel": f"{platform}:{channel}"})
                sent += 1
            self.store.set_control("alerts_seq", str(event.seq))
        return sent

    def _alert_text(self, event: Any, platform: str = "") -> str | None:
        payload = event.payload
        if event.kind == "recipe.state" and payload.get("to") == RecipeState.AWAITING_APPROVAL.value \
                and payload.get("from") != RecipeState.LIVE.value:
            from .adapters.render import plain

            recipe = self.store.recipe(payload["digest"])
            return "\n".join([
                f"🔔 Ready for your approval: {recipe.recipe_id} v{recipe.version}", "",
                plain(self.describe(recipe)), "",
                "To approve it from here:",
                "1. Send /capalis approve",
                # Telegram's choice buttons are numbered; the labels are listed in the message above them.
                "2. Tap 1 (Approve)" if platform == "telegram" else "2. Tap Approve"])
        if event.kind == "recipe.state" and payload.get("to") == RecipeState.QUARANTINED.value:
            recipe = self.store.recipe(payload["digest"])
            return "\n".join([
                f"⛔ {recipe.recipe_id} v{recipe.version} has stopped itself", "",
                f"Why: {payload.get('reason', '') or 'something it depends on changed'}.",
                "Nothing more will be written until you act.", "",
                "To fix it:",
                f'1. In Hermes, say: "Rebuild the {recipe.recipe_id} Recipe"',
                "2. Approve the new version when this chat asks you"])
        if event.kind != "run.state" or event.run_id is None:
            return None
        run = self.store.run(event.run_id)
        short = run.run_id[:8]
        if run.mode is not Mode.LIVE:
            return None
        target, detail = payload.get("to"), str(payload.get("detail") or "")
        if target == RunState.NEEDS_RECONCILIATION.value:
            step = next((a.step_id for a in self.store.unfinished_intents() if a.run_id == run.run_id), "<step>")
            return "\n".join([
                f"⚠️ {run.recipe_id} needs you (run {short})", "",
                "A write may or may not have happened, and Capalis can't check it by itself.",
                f"Details: {detail}", "",
                "Look at where it was going, then send one of:",
                f"• /capalis resolve {short} {step} applied",
                f"• /capalis resolve {short} {step} not-applied"])
        if target == RunState.WAITING.value and "confirmation" in detail:
            return "\n".join([
                f"⏳ {run.recipe_id} is waiting for you (run {short})", "",
                detail, "",
                f"When you're ready, send: /capalis confirm {short} <step>"])
        if target == RunState.FAILED.value:
            return "\n".join([
                f"❌ {run.recipe_id} did not finish (run {short})", "",
                detail, "",
                f"See every step: /capalis evidence {short}"])
        return None

    # -- Status and evidence ------------------------------------------------------------------------

    def describe(self, recipe: RecipeSpec) -> dict[str, Any]:
        from .core.expressions import Const

        steps = []
        for step in recipe.steps:
            origin, path = step.inputs.get("origin"), step.inputs.get("path")
            url = (f"{origin.value}{path.value}" if step.capability_id == "capalis:http.get"
                   and isinstance(origin, Const) and isinstance(path, Const) else None)
            steps.append({"id": step.step_id, "kind": step.kind.value, "capability": step.capability_id,
                          "url": url,
                          "effect": step.effect.value,
                          "target": None if step.target is None else str(step.target),
                          "when": step.when is not None, "after": list(step.depends_on),
                          "checks": [{"id": c.contract_id, "probe": c.probe, "level": c.level.value}
                                     for c in step.verify],
                          "unverifiable_reason": step.unverifiable_reason,
                          "wait": None if step.wait is None else (
                              f"{_condition_text(condition_to_wire(step.wait.until))}, checking every "
                              f"{step.wait.poll_interval_s}s for up to {step.wait.deadline_s // 86400 or 1} day(s)")})
        names = self.store.names()
        changes: list[str] | None = None
        if recipe.previous_digest:
            from .core.diff import diff_recipes

            try:
                changes = diff_recipes(self.store.recipe(recipe.previous_digest), recipe)
            except StoreError:
                changes = None
        return {"recipe_id": recipe.recipe_id, "version": recipe.version, "digest": recipe.digest,
                "changes_since_previous": changes,
                "names": {key: label for key, label in names.items()
                          if key in {str(t) for t in recipe.targets}
                          or key.split(":", 1)[1] in {*recipe.trigger.senders, recipe.trigger.source.split(":")[-1]}},
                "short_digest": recipe.digest[7:19], "state": self.store.recipe_state(recipe.digest).value,
                "intent": recipe.intent, "trigger": {"kind": recipe.trigger.kind.value, "source": recipe.trigger.source,
                                                     "senders": sorted(recipe.trigger.senders),
                                                     "schedule": recipe.trigger.schedule},
                "effects": sorted(e.value for e in recipe.effects), "targets": sorted(str(t) for t in recipe.targets),
                "steps": steps,
                "budgets": {"max_writes_per_run": recipe.budgets.max_writes_per_run,
                            "max_runs_per_day": recipe.budgets.max_runs_per_day,
                            "max_runs_per_hour": recipe.trigger.max_runs_per_hour}}

    def recipes(self) -> list[dict[str, Any]]:
        """The latest version of each Recipe; older versions are history, kept in the event log."""
        seen: dict[str, dict[str, Any]] = {}
        for recipe_id, version, digest, state in self.store.recipes():
            seen[recipe_id] = {"recipe_id": recipe_id, "version": version, "short_digest": digest[7:19],
                               "state": state.value, "digest": digest}
        registry = None
        for entry in seen.values():
            digest = entry.pop("digest")
            if entry["state"] == RecipeState.PAUSED.value:
                entry["reason"] = self._state_reason(digest)
            elif entry["state"] == RecipeState.QUARANTINED.value:
                # A change that is still there explains itself best (and in plain words, even for reasons
                # recorded by older versions); otherwise the reason recorded at the time stands.
                registry = registry or self.descriptors()
                drift = detect_drift(self.store.recipe(digest), registry)
                entry["reason"] = drift.plain() if drift.blocking else self._state_reason(digest)
        return list(seen.values())

    def status(self) -> dict[str, Any]:
        # Sweep first so status never shows a Recipe as live after its dependencies changed.
        self.drift_sweep()
        unfinished = self.store.unfinished_intents()
        latest = self.recipes()
        quarantined = [{"recipe_id": r["recipe_id"], "version": r["version"], "reason": r.get("reason", "")}
                       for r in latest if r["state"] == RecipeState.QUARANTINED.value]
        return {"globally_paused": self.store.global_paused(), "recipes": latest,
                "quarantined": quarantined,
                "recent_runs": [self.run_line(r) for r in self.store.runs(limit=10)],
                "needs_attention": [{"run_id": a.run_id, "step": a.step_id, "state": a.state.value,
                                     "detail": a.detail} for a in unfinished],
                "capabilities": {cid: cap.availability.value for cid, cap in self.descriptors().items()},
                "event_log_intact": self.store.verify_chain()[0]}

    def find_run(self, run_id: str) -> str:
        """Accept a full run id or an unambiguous prefix of at least 6 characters."""
        if len(run_id) >= 32:
            return run_id
        recent = self.store.runs(limit=1000) if len(run_id) >= 6 else []
        matches = [r.run_id for r in recent if r.run_id.startswith(run_id)]
        if len(matches) != 1:
            raise ServiceError(f"no single run starts with {run_id!r}")
        return matches[0]

    def evidence(self, run_id: str) -> dict[str, Any]:
        run = self.store.run(self.find_run(run_id))
        run_id = run.run_id
        steps = [{"step": a.step_id, "attempt": a.attempt, "state": a.state.value, "target": a.target,
                  "detail": a.detail, "output": a.output} for a in self.store.latest_attempts(run_id).values()]
        observations = [{"check": o["contract_id"], "step": o["step_id"], "probe": o["probe_id"],
                         "target": o["target"], "level": o["level"], "status": o["status"], "detail": o["detail"],
                         "error": o["error"], "observed_at": o["observed_at"]} for o in self.store.observations(run_id)]
        intact, broken_at = self.store.verify_chain()
        return {**self.run_line(run), "steps": steps, "independent_checks": observations,
                "events": len(self.store.events(run_id)), "event_log_intact": intact, "event_log_broken_at": broken_at}

    # -- Contracts for other Hermes tools -----------------------------------------------------------

    def propose_contract(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """Untrusted until tested and approved by the owner; proposing never makes a tool runnable."""
        from .core.contracts import parse_contract

        spec, problems = parse_contract(raw)
        if spec is None:
            return {"ok": False, "problems": problems}
        self.store.save_contract(spec.capability_id, spec.to_wire(), spec.digest)
        return {"ok": True, "capability": spec.capability_id, "state": self._contract(spec.capability_id)["state"],
                "next": f"Ask the owner to run /capalis contract test {spec.capability_id}, look at the sample "
                        f"output, then /capalis contract approve {spec.capability_id}."}

    def test_contract(self, capability_id: str, actor: Actor) -> dict[str, Any]:
        from .adapters.hermes_tools import parse_reply
        from .core import schema as jsonschema
        from .core.contracts import parse_contract

        self.authorize_actor(actor)
        row = self._contract(capability_id)
        spec, _ = parse_contract(row["spec"])
        if spec is None or self.dispatch is None:
            raise ServiceError("contract tests need the Hermes runtime")
        if spec.plugin and self.has_plugin is not None and not self.has_plugin(spec.plugin):
            raise ServiceError(f"the plugin {spec.plugin} is not installed and enabled")
        try:
            result = parse_reply(spec, self.dispatch(spec.tool, dict(spec.sample_inputs)))
        except Exception as exc:
            result = InvokeResult(Outcome.RETRYABLE, error=f"{type(exc).__name__}: {exc}")
        problems = [result.error or "failed"] if result.outcome is not Outcome.OK else \
            jsonschema.validate(dict(result.output or {}), spec.output_schema)
        sample = json.dumps(result.output or {}, default=str)[:800]
        test = {"passed": not problems, "problems": problems[:5], "sample_output": sample, "at": self.store.clock()}
        self.store.set_contract_state(capability_id, "tested" if not problems else "proposed", actor=actor.id,
                                      test=test)
        return {"capability": capability_id, **test}

    def approve_contract(self, capability_id: str, actor: Actor) -> str:
        """The owner attests the tool only reads, having seen its schema and a passing test."""
        self.authorize_actor(actor)
        row = self._contract(capability_id)
        if row["state"] != "tested" or not (row["test"] or {}).get("passed"):
            raise ServiceError(f"run /capalis contract test {capability_id} first; only a passing test can be approved")
        self.store.set_contract_state(capability_id, "approved", actor=actor.id)
        self._load_contracts()
        return (f"{capability_id} can now be used in Recipes as a read step or an independent check. Revoke it with "
                f"/capalis contract revoke {capability_id}; Recipes that use it are then quarantined.")

    def revoke_contract(self, capability_id: str, actor: Actor) -> str:
        self.authorize_actor(actor)
        self._contract(capability_id)
        self.store.set_contract_state(capability_id, "revoked", actor=actor.id)
        self._load_contracts()
        self.drift_sweep()
        return f"{capability_id} is revoked; Recipes that use it are quarantined."

    def contracts(self) -> list[dict[str, Any]]:
        return [{"capability": c["capability_id"], "tool": c["spec"]["tool"], "plugin": c["spec"]["plugin"],
                 "summary": c["spec"]["summary"], "state": c["state"], "test": c["test"]}
                for c in self.store.contracts()]

    def _contract(self, capability_id: str) -> dict[str, Any]:
        for row in self.store.contracts():
            if row["capability_id"] == capability_id:
                return row
        raise ServiceError(f"no contract for {capability_id}")

    def _load_contracts(self) -> None:
        if self.dispatch is None or not isinstance(self.adapters, dict):
            return
        from .adapters.hermes_tools import HermesToolAdapter
        from .core.contracts import parse_contract

        for row in self.store.contracts():
            cid = row["capability_id"]
            spec, _ = parse_contract(row["spec"])
            if row["state"] == "approved" and spec is not None:
                probe = self.has_plugin
                available: Callable[[], bool] = (functools.partial(probe, spec.plugin) if spec.plugin and probe
                                                 else (lambda: True))
                self.adapters[cid] = HermesToolAdapter(spec, self.dispatch, available)
            elif cid in self.adapters and cid.startswith("hermes:"):
                del self.adapters[cid]

    # -- Export and import --------------------------------------------------------------------------

    RECIPE_FORMAT = "capalis-recipe/1"

    def export_recipe(self, recipe_id: str) -> dict[str, Any]:
        """A shareable Recipe (FR-MEM-04): the plan only. No credentials, approvals, payloads or run history."""
        from .core.wire import wire_to_draft

        recipe, _ = self._latest(recipe_id)
        wire = recipe.to_wire()
        return {"format": self.RECIPE_FORMAT, "recipe_id": recipe.recipe_id, "version": recipe.version,
                "digest": recipe.digest, "intent": recipe.intent, "recipe": wire, "draft": wire_to_draft(wire),
                "names": self.describe(recipe)["names"],
                "note": "Contains no credentials, approvals, payloads or run history. Importing compiles it against "
                        "the importer's own capabilities; it must be shadow-qualified and approved there before "
                        "it can run."}

    def import_recipe(self, bundle: Mapping[str, Any], recipe_id: str, *, owner: str) -> dict[str, Any]:
        from .core.wire import WireError, recipe_from_wire, wire_to_draft

        if bundle.get("format") != self.RECIPE_FORMAT:
            raise ServiceError(f"not a {self.RECIPE_FORMAT} export")
        try:
            # The digest proves the plan is exactly what was exported; it is recompiled locally from there.
            original = recipe_from_wire(bundle["recipe"], expected_digest=str(bundle.get("digest")))
        except (WireError, KeyError, TypeError) as exc:
            raise ServiceError(f"the export is damaged or was edited: {exc}") from exc
        report = self.compile(recipe_id, wire_to_draft(original.to_wire()), owner=owner)
        report["imported_from"] = {"recipe_id": original.recipe_id, "version": original.version,
                                   "digest": original.digest}
        return report

    def export_evidence(self, run_id: str) -> dict[str, Any]:
        """A reviewer-friendly evidence bundle for one run (FR-VER-07), written beside the plugin data if possible."""
        import json as _json

        evidence = self.evidence(run_id)
        run = self.store.run(evidence["run_id"])
        recipe = self.store.recipe(run.recipe_digest)
        grant = self.store.active_grant(recipe.digest)
        bundle = {
            "format": "capalis-evidence/1", "generated_at": self.store.clock(), "run": evidence,
            "recipe": {"recipe_id": recipe.recipe_id, "version": recipe.version, "digest": recipe.digest,
                       "intent": recipe.intent, "definition": recipe.to_wire()},
            "approval": None if grant is None else {"actor": grant.actor, "issued_at": grant.issued_at,
                                                     "expires_at": grant.expires_at,
                                                     "effects": sorted(e.value for e in grant.effects),
                                                     "targets": sorted(str(t) for t in grant.targets)},
            "events": [{"seq": e.seq, "at": e.at, "kind": e.kind, "payload": e.payload, "hash": e.hash}
                       for e in self.store.events(evidence["run_id"])],
            "limits": ["Checks are V1 (an independent read from the same service) unless marked V2.",
                       "The event log's hash chain detects edits; it cannot stop someone who rewrites the whole "
                       "database.", "Payloads and outputs were redacted before they were stored."],
        }
        if self.export_dir is not None:
            from pathlib import Path

            folder = Path(self.export_dir())
            folder.mkdir(parents=True, exist_ok=True)
            stem = folder / f"evidence-{evidence['run_id'][:8]}"
            stem.with_suffix(".json").write_text(_json.dumps(bundle, indent=2, default=str) + "\n")
            from .adapters.render import evidence as render_evidence

            stem.with_suffix(".md").write_text(f"# Capalis evidence: run {evidence['run_id'][:8]}\n\n```text\n"
                                               f"{render_evidence(evidence)}\n```\n\nRecipe {recipe.recipe_id} "
                                               f"v{recipe.version} ({recipe.digest}).\n")
            bundle["files"] = [str(stem.with_suffix(".md")), str(stem.with_suffix(".json"))]
        return bundle

    # -- Gateway intake and background work ---------------------------------------------------------

    def on_message(self, *, platform: str, chat_id: str, sender: str, message_id: str, author: str,
                   text: str) -> Submission | None:
        """Admit a chat message for every Recipe listening on that channel; None when none is."""
        source = f"{platform}:channel:{chat_id}"
        result: Submission | None = None
        for recipe_id in sorted({row[0] for row in self.store.recipes()}):
            picked = self._listening(recipe_id)
            if picked is None or picked[0].trigger.source != source:
                continue
            recipe, mode = picked
            sub = self.runner.submit(recipe, source=source, sender=sender, mode=mode, advance=False,
                                     payload={"message_id": message_id, "author": author, "text": text})
            if sub.run_id is not None and sub.code == "queued":
                self._enqueue(sub.run_id)
            result = result if result is not None and result.accepted else sub
        return result

    def _enqueue(self, run_id: str) -> None:
        self._work.put(run_id)
        self.ensure_worker()

    def ensure_worker(self, interval_s: float = 30.0) -> None:
        with self._worker_lock:
            if self._stopping.is_set() or (self._worker is not None and self._worker.is_alive()):
                return
            # Start the alert cursor before anything can sweep, so a quarantine found at startup is reported.
            if (self.alert_channel() or "").strip() and self.store.control("alerts_seq") is None:
                self.store.set_control("alerts_seq", str(self.store.last_event_seq()))
            self._worker = threading.Thread(target=self._work_loop, args=(interval_s,), name="capalis-runner",
                                            daemon=True)
            self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        self._work.put("")
        worker = self._worker
        if worker is not None:
            worker.join(timeout)

    def _work_loop(self, interval_s: float) -> None:
        next_tick = 0.0
        while not self._stopping.is_set():
            try:
                run_id = self._work.get(timeout=max(0.1, next_tick - time.monotonic()))
                if run_id:
                    self.runner.advance(run_id)
            except queue.Empty:
                pass
            except Exception:  # keep the worker alive; the run stays durable and is retried by tick
                pass
            if time.monotonic() >= next_tick and not self._stopping.is_set():
                with contextlib.suppress(Exception):
                    self.drift_sweep()
                with contextlib.suppress(Exception):
                    self.fire_schedules()
                with contextlib.suppress(Exception):
                    self.runner.tick()
                with contextlib.suppress(Exception):
                    self.send_alerts()
                next_tick = time.monotonic() + interval_s

    # -- Helpers ------------------------------------------------------------------------------------

    def _listening(self, recipe_id: str) -> tuple[RecipeSpec, Mode] | None:
        """The version that handles real events: the live one, else a qualified one in shadow mode.
        A paused live version holds its events rather than letting an unapproved version take them."""
        live = self.store.version_in_state(recipe_id, RecipeState.LIVE)
        if live is not None:
            return live, Mode.LIVE
        paused = self.store.version_in_state(recipe_id, RecipeState.PAUSED)
        if paused is not None:
            return paused, Mode.LIVE
        for state in (RecipeState.AWAITING_APPROVAL, RecipeState.SHADOW_QUALIFIED):
            candidate = self.store.version_in_state(recipe_id, state)
            if candidate is not None:
                return candidate, Mode.SHADOW
        return None

    def _state_reason(self, digest: str) -> str:
        for event in reversed(self.store.events()):
            if event.kind == "recipe.state" and event.payload.get("digest") == digest:
                return str(event.payload.get("reason") or "")
        return ""

    def _latest(self, recipe_id: str) -> tuple[RecipeSpec, RecipeState]:
        latest = self.store.latest(recipe_id)
        if latest is None:
            raise ServiceError(f"no Recipe named {recipe_id}")
        return latest

    def run_line(self, run: Any) -> dict[str, Any]:
        recipe_id = run.recipe_id
        return {"run_id": run.run_id, "recipe_id": recipe_id, "mode": run.mode.value, "state": run.state.value,
                "detail": run.detail, "started_at": run.started_at, "resume_at": run.resume_at}

    def _submission(self, sub: Submission) -> dict[str, Any]:
        report: dict[str, Any] = {"accepted": sub.accepted, "code": sub.code, "detail": sub.detail}
        if sub.run_id is not None:
            report["run"] = self.evidence(sub.run_id)
        return report


def _condition_text(wire: Mapping[str, Any]) -> str:
    if "all" in wire:
        return " and ".join(_condition_text(item) for item in wire["all"])
    if "any" in wire:
        return " or ".join(_condition_text(item) for item in wire["any"])
    if "not" in wire:
        return f"not ({_condition_text(wire['not'])})"
    value = f" {wire['value']}" if "value" in wire else ""
    return f"{wire['ref']} {wire['op'].replace('_', ' ')}{value}"


__all__ = ["Actor", "Capalis", "IllegalTransition", "ServiceError", "StoreError"]
