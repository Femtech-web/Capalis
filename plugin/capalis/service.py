"""Capalis application service: what the chat tools, slash commands and gateway hook call.

It is independent of Hermes so it can be tested directly. Approval, resume, confirmation and manual
resolution are only reachable through ``Actor``-checked methods that the slash commands call; the
model-facing tools never expose them (I-02).
"""

from __future__ import annotations

import contextlib
import functools
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
from .core.lifecycle import IllegalTransition, RecipeState, RunState
from .core.matching import Requirement, match_requirement
from .core.model import Effect, EvidenceLevel, RecipeSpec, TriggerKind, grant_for
from .core.policy import Mode
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
                 directory: Any = None) -> None:
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
            match = match_requirement(requirement, registry)
            results.append({
                "need_id": requirement.need_id, "recommended": match.recommended, "rationale": match.rationale,
                "candidates": [{"id": a.capability_id, "can_run": a.executable, "excluded_because": list(a.exclusions),
                                "factors": {f.name: {"summary": f.summary, "basis": f.basis.value} for f in a.factors},
                                "setup": a.setup}
                               for a in match.candidates],
            })
        return results

    def bug_report_template(self, *, repo: str, slack_channel: str, discord_channel: str, reporters: Sequence[str],
                            reply_when_closed: bool = True) -> dict[str, Any]:
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
        repo_id = resolve("github.repo", repo, lambda: d.github_repo(repo))
        slack_id = resolve("slack.channel", slack_channel, lambda: d.slack_channel(slack_channel))
        discord_id = resolve("discord.channel", discord_channel, lambda: d.discord_channel(discord_channel))
        people = [resolve("discord.user", who, functools.partial(d.discord_user, who, discord_id)) if d else who
                  for who in reporters]
        self.store.set_names(labels)
        draft = bug_report(repo=repo_id, slack_channel=slack_id, discord_channel=discord_id, reporters=people,
                           reply_when_closed=reply_when_closed)
        return {"draft": draft, "resolved": resolved, "format": DRAFT_FORMAT,
                "notes": [f"{r['name']}: {r['note']}" for r in resolved if r["note"]]}

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
        report["next"] = (f"Ask the owner to review and type /capalis approve {recipe_id} {recipe.digest[7:19]}"
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
        return f"{recipe_id} v{recipe.version} is paused."

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
        days = (grant.expires_at - grant.issued_at) // 86400
        return (f"Approved {recipe_id} v{recipe.version} for {days} days: "
                f"{', '.join(sorted(e.value for e in grant.effects))} on "
                f"{', '.join(sorted(str(t) for t in grant.targets))}, triggered by {recipe.trigger.source}.")

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
            self.store.transition(recipe.digest, RecipeState.QUARANTINED, reason=drift.summary())
            raise ServiceError(f"dependencies changed while paused, so it is quarantined: {drift.summary()}")
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
        return [{"area": c.area, "ok": c.ok, "detail": c.detail, "fix": c.fix}
                for c in readiness(self.directory, targets, discord)]

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
                    self.store.transition(recipe.digest, RecipeState.QUARANTINED, reason=report.summary())
                    quarantined.append({"recipe_id": recipe_id, "version": str(recipe.version),
                                        "reason": report.summary()})
        return quarantined

    # -- Status and evidence ------------------------------------------------------------------------

    def describe(self, recipe: RecipeSpec) -> dict[str, Any]:
        steps = []
        for step in recipe.steps:
            steps.append({"id": step.step_id, "kind": step.kind.value, "capability": step.capability_id,
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
        return {"recipe_id": recipe.recipe_id, "version": recipe.version, "digest": recipe.digest,
                "names": {key: label for key, label in names.items()
                          if key in {str(t) for t in recipe.targets}
                          or key.split(":", 1)[1] in {*recipe.trigger.senders, recipe.trigger.source.split(":")[-1]}},
                "short_digest": recipe.digest[7:19], "state": self.store.recipe_state(recipe.digest).value,
                "intent": recipe.intent, "trigger": {"kind": recipe.trigger.kind.value, "source": recipe.trigger.source,
                                                     "senders": sorted(recipe.trigger.senders)},
                "effects": sorted(e.value for e in recipe.effects), "targets": sorted(str(t) for t in recipe.targets),
                "steps": steps,
                "budgets": {"max_writes_per_run": recipe.budgets.max_writes_per_run,
                            "max_runs_per_day": recipe.budgets.max_runs_per_day,
                            "max_runs_per_hour": recipe.trigger.max_runs_per_hour}}

    def recipes(self) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for recipe_id, version, digest, state in self.store.recipes():
            seen[recipe_id] = {"recipe_id": recipe_id, "version": version, "short_digest": digest[7:19],
                               "state": state.value}
        return list(seen.values())

    def status(self) -> dict[str, Any]:
        unfinished = self.store.unfinished_intents()
        quarantined = [{"recipe_id": rid, "version": version, "reason": self._state_reason(digest)}
                       for rid, version, digest, state in self.store.recipes() if state is RecipeState.QUARANTINED]
        return {"globally_paused": self.store.global_paused(), "recipes": self.recipes(),
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
                    self.runner.tick()
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
