"""Controlled runner: the only path from a compiled Recipe to an external effect (D-005, D-016).

A run is a durable state machine. ``advance`` moves it as far as it safely can and then parks, and every
decision is re-derived from the store, so a crash at any point resumes through the same code. Before a
side effect the runner re-reads lifecycle, pause and approval state, re-checks pins, records the write
intent, and only then dispatches. An attempt that may have reached the provider is never replayed
blindly: it is reconciled by key, or the run stops for an operator.
"""

from __future__ import annotations

import contextlib
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..core import schema as jsonschema
from ..core.drift import DriftReport, detect_drift
from ..core.expressions import MISSING, Ref, evaluate, resolve_ref, resolve_value
from ..core.ids import idempotency_key
from ..core.lifecycle import RUN_TERMINAL, STEP_TERMINAL, IllegalTransition, RecipeState, RunState, StepState
from ..core.model import (
    CapabilityDescriptor,
    EvidenceLevel,
    IdempotencyStrategy,
    OutcomeContract,
    RecipeSpec,
    StepKind,
    StepSpec,
)
from ..core.policy import (
    Decision,
    Mode,
    RunContext,
    StepContext,
    TriggerEvent,
    authorize_probe,
    authorize_run,
    authorize_step,
    grant_problem,
)
from ..core.synth import placeholder
from ..core.verification import (
    ContractStatus,
    ContractVerdict,
    Observation,
    OutcomeStatus,
    evaluate_contract,
    summarize,
)
from ..storage.db import ConflictError, RateLimited, StepAttempt, Store
from .ports import Adapter, Invocation, InvokeResult, Outcome

_HAS_OUTPUT = frozenset({StepState.COMPLETED, StepState.OBSERVED_VERIFIED, StepState.SIMULATED, StepState.UNVERIFIED})
_BLOCKS_DEPENDENTS = frozenset({StepState.BLOCKED, StepState.FAILED, StepState.UNVERIFIED, StepState.UNKNOWN_EFFECT})
_FINAL_CONTRACT = frozenset({ContractStatus.MET, ContractStatus.NOT_MET})
# Denials that hold a run until a person acts, rather than failing it.
_HOLD_CODES = frozenset({"global_pause", "needs_confirmation"})
_DEPENDENCY_BLOCK = "depends on "



@dataclass(frozen=True, slots=True)
class RunnerConfig:
    lease_s: int = 900  # longer than the 600 s step timeout ceiling, so a live call never loses its lease
    max_concurrent_runs: int = 4
    inline_wait_s: int = 15
    reprobe_s: tuple[int, ...] = (2, 5, 15, 30, 60, 300)
    reconcile_retry_s: int = 60
    reconcile_settle_s: int = 60


@dataclass(frozen=True, slots=True)
class Submission:
    accepted: bool
    code: str
    detail: str
    run_id: str | None = None
    state: RunState | None = None
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class _Park:
    state: RunState
    resume_at: int | None
    detail: str


class _Abandon(Exception):
    """Another executor moved this run first; stop without touching it."""


class Runner:
    def __init__(self, store: Store, adapters: Mapping[str, Adapter], *, config: RunnerConfig | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.store = store
        self.adapters = adapters
        self.config = config or RunnerConfig()
        self.sleep = sleep
        self.owner = f"runner-{uuid.uuid4().hex[:12]}"
        self._lease = threading.local()
        self._slots = threading.BoundedSemaphore(self.config.max_concurrent_runs)

    @property
    def clock(self) -> Callable[[], int]:
        return self.store.clock

    def descriptors(self) -> dict[str, CapabilityDescriptor]:
        return {cid: adapter.descriptor for cid, adapter in self.adapters.items()}

    def drift(self, recipe: RecipeSpec) -> DriftReport:
        return detect_drift(recipe, self.descriptors())

    # -- Ingress ------------------------------------------------------------------------------------

    def submit(self, recipe: RecipeSpec, *, source: str, sender: str, payload: Mapping[str, Any],
               mode: Mode = Mode.LIVE, dedupe_value: str | None = None, cause_id: str | None = None,
               advance: bool = True) -> Submission:
        """Admit a trigger and, unless ``advance`` is False, run it. Admission is durable on its own: a run
        recorded here is picked up by ``tick`` even if this process stops before advancing it."""
        def reject(code: str, detail: str) -> Submission:
            self.store.append_event("trigger.rejected", {"recipe_digest": recipe.digest, "mode": mode.value,
                                                         "source": source, "code": code, "detail": detail})
            return Submission(False, code, detail)

        problems = jsonschema.validate(payload, recipe.trigger.payload_schema)
        if problems:
            return reject("payload_invalid", "; ".join(problems[:3]))
        if dedupe_value is None and recipe.trigger.dedupe_key is not None:
            dedupe_value = _dedupe(payload, recipe.trigger.dedupe_key)
        now = self.clock()
        drift = self.drift(recipe)
        ctx = RunContext(now=now, mode=mode, recipe_state=self.store.recipe_state(recipe.digest),
                         global_paused=self.store.global_paused(), grant=self.store.active_grant(recipe.digest, now),
                         drift=drift, runs_last_hour=self.store.runs_since(recipe.recipe_id, now - 3600 + 1),
                         runs_today=self.store.runs_since(recipe.recipe_id, now - 86400 + 1))
        decision = authorize_run(recipe, TriggerEvent(source, sender, dedupe_value), ctx)
        if not decision.allowed:
            if decision.code == "drift" and mode is Mode.LIVE:
                self._quarantine(recipe, drift.summary())
            return reject(decision.code, decision.detail)
        if dedupe_value is None:
            return reject("no_dedupe_value", "the trigger has no value to deduplicate on")
        limits = (recipe.trigger.max_runs_per_hour, recipe.budgets.max_runs_per_day) if mode is Mode.LIVE else None
        try:
            run, created = self.store.start_run(recipe, mode=mode, trigger_source=source, sender=sender,
                                                dedupe_value=dedupe_value, payload=payload, cause_id=cause_id,
                                                limits=limits)
        except RateLimited as exc:
            return reject("rate", str(exc))
        if not created:
            return Submission(True, "duplicate", "this trigger already started a run", run.run_id, run.state, True)
        if not advance:
            return Submission(True, "queued", "admitted", run.run_id, run.state)
        state = self.advance(run.run_id)
        return Submission(True, "ok", self.store.run(run.run_id).detail, run.run_id, state)

    # -- Execution ----------------------------------------------------------------------------------

    def tick(self) -> int:
        """Advance every run that is due: parked runs whose time has come and runs whose executor died."""
        due = self.store.due_runs()
        for run_id in due:
            self.advance(run_id)
        return len(due)

    def advance(self, run_id: str) -> RunState:
        # A lease token per call, so two threads of this process cannot drive the same run either.
        token = f"{self.owner}-{uuid.uuid4().hex[:8]}"
        with self._slots:
            if not self.store.claim_run(run_id, token, self.config.lease_s):
                return self.store.run(run_id).state
            self._lease.token = token
            try:
                while True:
                    park = self._advance_once(run_id)
                    if park is None:
                        break
                    delay = None if park.resume_at is None else park.resume_at - self.clock()
                    if park.state is RunState.WAITING and delay is not None and delay <= self.config.inline_wait_s:
                        self.sleep(max(0, delay))
                        continue
                    self.store.set_run_state(run_id, park.state, detail=park.detail, resume_at=park.resume_at)
                    break
            except (_Abandon, ConflictError, IllegalTransition):
                pass
            finally:
                self.store.release_run(run_id, token)
        return self.store.run(run_id).state

    def confirm(self, run_id: str, step_id: str, *, actor: str) -> RunState:
        self.store.confirm_step(run_id, step_id, actor=actor)
        return self.advance(run_id)

    def cancel(self, run_id: str, *, actor: str) -> RunState:
        run = self.store.run(run_id)
        if run.state in RUN_TERMINAL:
            return run.state
        for attempt in self.store.latest_attempts(run_id).values():
            if attempt.state in (StepState.PLANNED, StepState.INTENT_RECORDED):
                self.store.set_step_state(run_id, attempt.step_id, attempt.attempt, StepState.BLOCKED,
                                          detail=f"run cancelled by {actor}")
        return self.store.set_run_state(run_id, RunState.CANCELLED, detail=f"cancelled by {actor}")

    def resolve_unknown(self, run_id: str, step_id: str, *, applied: bool, actor: str) -> RunState:
        """An operator's answer for an effect Capalis could not check. It is recorded as a human
        statement, never as an independent observation."""
        attempt = self.store.latest_attempts(run_id).get(step_id)
        if attempt is None or attempt.state is not StepState.UNKNOWN_EFFECT:
            raise IllegalTransition(f"{step_id} has no unknown effect to resolve")
        if applied:
            self.store.set_step_state(run_id, step_id, attempt.attempt, StepState.UNVERIFIED,
                                      detail=f"{actor} stated the effect was applied; not independently observed")
        else:
            self.store.set_step_state(run_id, step_id, attempt.attempt, StepState.FAILED,
                                      detail=f"{actor} stated the effect was not applied")
        return self.advance(run_id)

    def _advance_once(self, run_id: str) -> _Park | None:
        run = self.store.run(run_id)
        if run.state in RUN_TERMINAL:
            return None
        recipe = self.store.recipe(run.recipe_digest)
        if run.state is not RunState.RUNNING:
            self.store.set_run_state(run_id, RunState.RUNNING, detail="resumed" if run.state is not RunState.QUEUED
                                     else "started")
        run = self.store.run(run_id)
        scope: dict[str, Any] = {"trigger": run.payload, "steps": {}}
        states: dict[str, StepState] = {}
        for step in recipe.steps:
            park = self._step(recipe, run, step, scope, states)
            if park is not None:
                return park
        self._finish(recipe, run)
        return None

    def _step(self, recipe: RecipeSpec, run: Any, step: StepSpec, scope: dict[str, Any],
              states: dict[str, StepState]) -> _Park | None:
        while True:
            attempt = self.store.latest_attempts(run.run_id).get(step.step_id)
            if attempt is not None and attempt.state in STEP_TERMINAL:
                states[step.step_id] = attempt.state
                if attempt.state in _HAS_OUTPUT and attempt.output is not None:
                    scope["steps"][step.step_id] = attempt.output
                return None
            park = self._progress(recipe, run, step, attempt, scope, states)
            if park is not None:
                return park

    def _progress(self, recipe: RecipeSpec, run: Any, step: StepSpec, attempt: StepAttempt | None,
                  scope: dict[str, Any], states: Mapping[str, StepState]) -> _Park | None:
        if attempt is None or attempt.state is StepState.PLANNED:
            return self._begin(recipe, run, step, attempt, scope, states)
        if attempt.state is StepState.INTENT_RECORDED:
            # Recorded but never dispatched (dispatch is journaled first), so it is safe to send now.
            return self._dispatch_write(recipe, run, step, attempt, scope)
        if attempt.state is StepState.DISPATCHED:
            if step.is_side_effect:
                self._set(run, attempt, StepState.UNKNOWN_EFFECT, "interrupted after dispatch; outcome unknown")
                return None
            self._set(run, attempt, StepState.FAILED, "interrupted before the read completed")
            return self._plan_retry(run, step, attempt, max(step.retry.max_attempts, 3), 0)
        if attempt.state is StepState.UNKNOWN_EFFECT:
            return self._reconcile(recipe, run, step, attempt, scope)
        if attempt.state is StepState.WAITING:
            scope["steps"][step.step_id] = attempt.output
            if step.kind is StepKind.WAIT:
                return self._poll_wait(recipe, run, step, attempt, scope)
            return self._verify(recipe, run, step, attempt, scope)
        raise IllegalTransition(f"unexpected step state {attempt.state.value}")

    # -- Steps --------------------------------------------------------------------------------------

    def _begin(self, recipe: RecipeSpec, run: Any, step: StepSpec, planned: StepAttempt | None,
               scope: dict[str, Any], states: Mapping[str, StepState]) -> _Park | None:
        number = 1 if planned is None else planned.attempt
        key = idempotency_key(recipe.recipe_id, run.dedupe_value, step.step_id) if step.is_side_effect else ""

        def mark(state: StepState, detail: str, output: Any = None) -> None:
            if planned is None:
                self.store.record_attempt(run.run_id, step.step_id, number, idempotency_key=key, effect=step.effect,
                                          target=step.target, state=state, detail=detail, output=output)
            else:
                self.store.set_step_state(run.run_id, step.step_id, number, state, detail=detail, output=output)

        blocked_by = [dep for dep in step.depends_on if states.get(dep) in _BLOCKS_DEPENDENTS]
        if blocked_by:
            mark(StepState.BLOCKED, f"{_DEPENDENCY_BLOCK}{', '.join(blocked_by)}, which did not succeed")
            return None
        if step.when is not None and not evaluate(step.when, scope):
            mark(StepState.SKIPPED, "condition not met")
            return None
        decision = self._authorize(recipe, run, step)
        if not decision.allowed:
            return self._denied(recipe, run, step, decision, mark)
        inputs, problem = self._inputs(step, scope)
        if problem is not None:
            mark(StepState.BLOCKED, problem)
            return None
        # In shadow mode, a step that follows a simulated write would observe something that never happened.
        after_simulated = run.mode is Mode.SHADOW and any(states.get(d) is StepState.SIMULATED for d in step.depends_on)
        if decision.simulate or after_simulated:
            descriptor = self.adapters[step.capability_id].descriptor
            detail = ("shadow run: write simulated, nothing sent" if decision.simulate
                      else "shadow run: follows a simulated write, so not run")
            mark(StepState.SIMULATED, detail, output=placeholder(descriptor.output_schema, f"{step.step_id}"))
            return None
        if step.is_side_effect:
            mark(StepState.INTENT_RECORDED, "write intent recorded")
            return None
        mark(StepState.DISPATCHED, "reading")
        attempt = self.store.latest_attempts(run.run_id)[step.step_id]
        return self._run_read(recipe, run, step, attempt, inputs, scope)

    def _dispatch_write(self, recipe: RecipeSpec, run: Any, step: StepSpec, attempt: StepAttempt,
                        scope: Mapping[str, Any]) -> _Park | None:
        decision = self._authorize(recipe, run, step)
        if not decision.allowed:
            return self._denied(recipe, run, step, decision,
                                lambda state, detail, output=None: self._set(run, attempt, state, detail))
        inputs, problem = self._inputs(step, scope)
        if problem is not None:
            self._set(run, attempt, StepState.BLOCKED, problem)
            return None
        if not self.store.claim_run(run.run_id, self._lease.token, self.config.lease_s):
            raise _Abandon
        # The CAS from intent_recorded to dispatched is what makes a second executor back off.
        self._set(run, attempt, StepState.DISPATCHED, "dispatching")
        result = self._invoke(step.capability_id, step, inputs, attempt.idempotency_key, side_effect=True)
        if result.outcome is Outcome.OK:
            output = dict(result.output or {})
            errors = jsonschema.validate(output, self.adapters[step.capability_id].descriptor.output_schema or {})
            if errors:
                self._set(run, attempt, StepState.UNKNOWN_EFFECT,
                          f"the provider answered but the result did not match its schema: {errors[0]}")
                return None
            return self._after_write(run, step, attempt, output, "provider reported success")
        if result.outcome is Outcome.AMBIGUOUS:
            self._set(run, attempt, StepState.UNKNOWN_EFFECT, result.error or "outcome unknown")
            return None
        self._set(run, attempt, StepState.FAILED, result.error or result.outcome.value)
        if result.outcome is Outcome.RETRYABLE:
            return self._plan_retry(run, step, attempt, step.retry.max_attempts, result.retry_after_s or 0)
        return None

    def _after_write(self, run: Any, step: StepSpec, attempt: StepAttempt, output: Mapping[str, Any],
                     how: str) -> _Park | None:
        if step.verify:
            self._set(run, attempt, StepState.WAITING, f"{how}; verifying independently", output=dict(output))
        else:
            self._set(run, attempt, StepState.UNVERIFIED,
                      f"{how}; not independently checked: {step.unverifiable_reason or 'no verifier'}",
                      output=dict(output))
        return None

    def _run_read(self, recipe: RecipeSpec, run: Any, step: StepSpec, attempt: StepAttempt,
                  inputs: Mapping[str, Any], scope: dict[str, Any]) -> _Park | None:
        result = self._invoke(step.capability_id, step, inputs, None, side_effect=False)
        output, error = self._read_output(step.capability_id, result)
        if step.kind is StepKind.WAIT:
            return self._wait_outcome(recipe, run, step, attempt, output, error, scope)
        if output is not None:
            self._set(run, attempt, StepState.COMPLETED, "read", output=output)
            return None
        self._set(run, attempt, StepState.FAILED, error or "read failed")
        if result.outcome is Outcome.RETRYABLE:
            return self._plan_retry(run, step, attempt, step.retry.max_attempts, result.retry_after_s or 0)
        return None

    def _poll_wait(self, recipe: RecipeSpec, run: Any, step: StepSpec, attempt: StepAttempt,
                   scope: dict[str, Any]) -> _Park | None:
        decision = self._authorize(recipe, run, step)
        if not decision.allowed:
            # A wait that is already under way can end only as failed, not blocked.
            return self._denied(recipe, run, step, decision,
                                lambda state, detail, output=None: self._set(run, attempt, StepState.FAILED, detail))
        inputs, problem = self._inputs(step, scope)
        if problem is not None:
            self._set(run, attempt, StepState.FAILED, problem)
            return None
        self._set(run, attempt, StepState.DISPATCHED, "polling")
        return self._run_read(recipe, run, step, self._latest(run, step), inputs, scope)

    def _wait_outcome(self, recipe: RecipeSpec, run: Any, step: StepSpec, attempt: StepAttempt,
                      output: Mapping[str, Any] | None, error: str | None, scope: dict[str, Any]) -> _Park | None:
        assert step.wait is not None
        now = self.clock()
        if output is not None:
            scope["steps"][step.step_id] = output
            if evaluate(step.wait.until, scope):
                self._set(run, attempt, StepState.COMPLETED, "wait condition met", output=dict(output))
                return None
        if run.mode is Mode.SHADOW:
            if output is None:
                self._set(run, attempt, StepState.FAILED, error or "read failed")
            else:
                self._set(run, attempt, StepState.COMPLETED,
                          "shadow run: polled once, condition not met yet; a live run would keep polling",
                          output=dict(output))
            return None
        if now + step.wait.poll_interval_s > attempt.recorded_at + step.wait.deadline_s:
            self._set(run, attempt, StepState.FAILED, "the wait condition was not met before its deadline",
                      output=None if output is None else dict(output))
            return None
        self._set(run, attempt, StepState.WAITING, error or "condition not met yet",
                  output=None if output is None else dict(output))
        return _Park(RunState.WAITING, now + step.wait.poll_interval_s, f"waiting for {step.step_id}")

    # -- Verification -------------------------------------------------------------------------------

    def _verify(self, recipe: RecipeSpec, run: Any, step: StepSpec, attempt: StepAttempt,
                scope: Mapping[str, Any]) -> _Park | None:
        now = self.clock()
        drift = self.drift(recipe)
        history: dict[str, list[dict[str, Any]]] = {}
        for row in self.store.observations(run.run_id):
            if row["step_id"] == step.step_id:
                history.setdefault(row["contract_id"], []).append(row)
        ctx = self._step_context(recipe, run)
        verdicts: list[ContractVerdict] = []
        next_probe: int | None = None
        for contract in step.verify:
            seen = history.get(contract.contract_id, [])
            if seen and ContractStatus(seen[-1]["status"]) in _FINAL_CONTRACT:
                last = seen[-1]
                verdicts.append(ContractVerdict(contract.contract_id, ContractStatus(last["status"]),
                                                EvidenceLevel(last["level"]), last["detail"], contract.required))
                continue
            decision = authorize_probe(recipe, step, contract.contract_id, ctx)
            if not decision.allowed:
                verdicts.append(ContractVerdict(contract.contract_id, ContractStatus.ERROR, None,
                                                f"check not allowed: {decision.detail}", contract.required))
                continue
            changed = [c for c in drift.changes if c.capability_id == contract.probe]
            if changed:
                change = changed[0]
                observation = self._observation(contract, now, error=f"the checker changed since approval: "
                                                f"{change.kind.value} ({change.before} -> {change.after})")
            else:
                observation = self._probe(contract, scope, now)
            verdict = evaluate_contract(contract, observation, now=now, started_at=attempt.recorded_at, scope=scope)
            self.store.record_observation(run.run_id, step.step_id, observation, verdict)
            deadline = attempt.recorded_at + contract.deadline_s
            if verdict.status not in _FINAL_CONTRACT and now < deadline and contract.required:
                wait = self.config.reprobe_s[min(len(seen), len(self.config.reprobe_s) - 1)]
                at = min(now + wait, deadline)
                next_probe = at if next_probe is None else min(next_probe, at)
            verdicts.append(verdict)
        required = [v for v in verdicts if v.required]
        failed = [v for v in required if v.status is ContractStatus.NOT_MET]
        if failed:
            self._set(run, attempt, StepState.FAILED,
                      "the independent check disagrees with the provider: "
                      + "; ".join(f"{v.contract_id}: {v.detail}" for v in failed))
            return None
        if next_probe is not None:
            return _Park(RunState.WAITING, next_probe, f"waiting to re-check {step.step_id}")
        outcome = summarize(verdicts)
        if outcome.status is OutcomeStatus.VERIFIED:
            self._set(run, attempt, StepState.OBSERVED_VERIFIED, "independently observed")
        else:
            self._set(run, attempt, StepState.UNVERIFIED, "; ".join(
                f"{v.contract_id}: {v.detail}" for v in required if v.status is not ContractStatus.MET)
                or "no required check")
        return None

    def _probe(self, contract: OutcomeContract, scope: Mapping[str, Any], now: int) -> Observation:
        adapter = self.adapters.get(contract.probe)
        if adapter is None:
            return self._observation(contract, now, error="the checker is not available")
        inputs = {name: value for name, ref in contract.inputs.items()
                  if (value := resolve_value(ref, scope)) is not MISSING}
        errors = jsonschema.validate(inputs, adapter.descriptor.input_schema)
        if errors:
            return self._observation(contract, now, error=f"the check could not be prepared: {errors[0]}")
        result = self._call(adapter, Invocation(contract.probe, contract.probe_target, inputs, None, 30),
                            side_effect=False)
        output, error = self._read_output(contract.probe, result)
        return self._observation(contract, now, values=output or {}, error=error)

    @staticmethod
    def _observation(contract: OutcomeContract, now: int, *, values: Mapping[str, Any] | None = None,
                     error: str | None = None) -> Observation:
        # The level is the contract's: the compiler only accepts probes that are independent of the write
        # and, for V2, on a different system (E_PROBE_NOT_INDEPENDENT, E_V2_NOT_CROSS_SYSTEM).
        return Observation(contract.contract_id, contract.probe, contract.probe_target, contract.level, now,
                           values or {}, error)

    # -- Reconciliation -----------------------------------------------------------------------------

    def _reconcile(self, recipe: RecipeSpec, run: Any, step: StepSpec, attempt: StepAttempt,
                   scope: Mapping[str, Any]) -> _Park | None:
        if step.idempotency is IdempotencyStrategy.NATIVE_KEY and attempt.attempt < step.retry.max_attempts:
            self._set(run, attempt, StepState.FAILED,
                      "outcome unknown; superseded by a replay under the same provider idempotency key")
            return self._plan_retry(run, step, attempt, step.retry.max_attempts, step.retry.backoff_s)
        if step.idempotency is not IdempotencyStrategy.RECONCILE:
            return _Park(RunState.NEEDS_RECONCILIATION, None,
                         f"{step.step_id} may or may not have happened and cannot be checked automatically; "
                         "resolve it before this run continues")
        write = self.adapters.get(step.capability_id)
        lookup = self.adapters.get(write.descriptor.reconcile_with or "") if write else None
        retry_at = self.clock() + self.config.reconcile_retry_s
        if write is None or lookup is None or lookup.descriptor.target is None or step.target is None:
            return _Park(RunState.NEEDS_RECONCILIATION, retry_at, f"cannot look up {step.step_id} right now")
        if any(c.capability_id == lookup.descriptor.id for c in self.drift(recipe).changes):
            return _Park(RunState.NEEDS_RECONCILIATION, None,
                         f"the lookup for {step.step_id} changed since approval; resolve it manually")
        if run.mode is Mode.LIVE:
            # The lookup is a read of the approved target: it needs a valid approval, not write budget.
            now = self.clock()
            grant = self.store.active_grant(recipe.digest, now)
            problem = grant_problem(grant, recipe, now)
            if problem is not None or grant is None or step.target not in grant.targets:
                detail = problem.detail if problem is not None else "the approval does not cover this target"
                return _Park(RunState.NEEDS_RECONCILIATION, None, f"cannot check {step.step_id}: {detail}")
        declared = lookup.descriptor.input_schema.get("properties") or {}
        # Providers that cannot store a key (Discord) identify the effect by the write's own inputs.
        written, _ = self._inputs(step, scope)
        inputs: dict[str, Any] = {name: value for name, value in written.items() if name in declared}
        inputs.update({lookup.descriptor.target.param: step.target.value, "idempotency_key": attempt.idempotency_key})
        if "since" in declared:
            inputs["since"] = attempt.recorded_at
        result = self._call(lookup, Invocation(lookup.descriptor.id, step.target, inputs, None, step.timeout_s),
                            side_effect=False)
        output, error = self._read_output(lookup.descriptor.id, result)
        if output is None:
            return _Park(RunState.NEEDS_RECONCILIATION, retry_at,
                         f"could not check whether {step.step_id} happened: {error}")
        if output.get("found") is True:
            resource = output.get("resource")
            schema = write.descriptor.output_schema or {}
            if not isinstance(resource, Mapping) or jsonschema.validate(dict(resource), schema):
                return _Park(RunState.NEEDS_RECONCILIATION, None,
                             f"found {step.step_id} by its key, but the record is unreadable; resolve it manually")
            return self._after_write(run, step, attempt, resource, "found by idempotency key after an unknown outcome")
        settled_at = attempt.recorded_at + self.config.reconcile_settle_s
        if self.clock() < settled_at:
            # Provider lists lag behind writes (observed on GitHub's REST API), so absence only counts
            # once the write has had time to show up.
            return _Park(RunState.WAITING, settled_at, f"checking again whether {step.step_id} happened")
        self._set(run, attempt, StepState.FAILED, "no effect found by idempotency key: it did not happen")
        return self._plan_retry(run, step, attempt, step.retry.max_attempts, step.retry.backoff_s)

    # -- Helpers ------------------------------------------------------------------------------------

    def _step_context(self, recipe: RecipeSpec, run: Any) -> StepContext:
        now = self.clock()
        return StepContext(now=now, mode=run.mode, recipe_state=self.store.recipe_state(recipe.digest),
                           global_paused=self.store.global_paused(),
                           grant=self.store.active_grant(recipe.digest, now), drift=self.drift(recipe),
                           trigger_source=run.trigger_source,
                           writes_dispatched=self.store.writes_dispatched(run.run_id),
                           confirmed_steps=self.store.confirmations(run.run_id))

    def _authorize(self, recipe: RecipeSpec, run: Any, step: StepSpec) -> Decision:
        if step.capability_id not in self.adapters:
            return Decision(False, "unavailable", f"{step.capability_id} is not available")
        return authorize_step(recipe, step, self._step_context(recipe, run))

    def _denied(self, recipe: RecipeSpec, run: Any, step: StepSpec, decision: Decision,
                mark: Callable[..., None]) -> _Park | None:
        if decision.code in _HOLD_CODES or (decision.code == "state"
                                            and self.store.recipe_state(recipe.digest) is RecipeState.PAUSED):
            return _Park(RunState.WAITING, None, f"{step.step_id} on hold: {decision.detail}")
        if decision.code == "drift" and run.mode is Mode.LIVE:
            self._quarantine(recipe, decision.detail)
        mark(StepState.BLOCKED, f"{decision.code}: {decision.detail}")
        return None

    def _inputs(self, step: StepSpec, scope: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
        descriptor = self.adapters[step.capability_id].descriptor
        inputs = {name: value for name, ref in step.inputs.items()
                  if (value := resolve_value(ref, scope)) is not MISSING}
        if descriptor.target is not None and step.target is not None \
                and inputs.get(descriptor.target.param) != step.target.value:
            return inputs, "the resolved target differs from the approved one"
        errors = jsonschema.validate(inputs, descriptor.input_schema)
        if errors:
            return inputs, "inputs do not satisfy the capability schema: " + "; ".join(errors[:3])
        return inputs, None

    def _invoke(self, capability_id: str, step: StepSpec, inputs: Mapping[str, Any], key: str | None, *,
                side_effect: bool) -> InvokeResult:
        call = Invocation(capability_id, step.target, inputs, key, step.timeout_s)
        return self._call(self.adapters[capability_id], call, side_effect=side_effect)

    @staticmethod
    def _call(adapter: Adapter, call: Invocation, *, side_effect: bool) -> InvokeResult:
        try:
            return adapter.invoke(call)
        except Exception as exc:  # an adapter bug must not be mistaken for a clean failure
            outcome = Outcome.AMBIGUOUS if side_effect else Outcome.RETRYABLE
            return InvokeResult(outcome, error=f"{type(exc).__name__}: {exc}")

    def _read_output(self, capability_id: str, result: InvokeResult) -> tuple[dict[str, Any] | None, str | None]:
        if result.outcome is not Outcome.OK:
            return None, result.error or result.outcome.value
        output = dict(result.output or {})
        errors = jsonschema.validate(output, self.adapters[capability_id].descriptor.output_schema or {})
        if errors:
            return None, f"returned output that does not match its schema: {errors[0]}"
        return output, None

    def _plan_retry(self, run: Any, step: StepSpec, attempt: StepAttempt, max_attempts: int,
                    delay_s: int) -> _Park | None:
        if attempt.attempt >= max_attempts:
            return None
        self.store.record_attempt(run.run_id, step.step_id, attempt.attempt + 1,
                                  idempotency_key=attempt.idempotency_key, effect=step.effect, target=step.target,
                                  state=StepState.PLANNED, detail="retry planned")
        delay = max(delay_s, step.retry.backoff_s)
        return _Park(RunState.WAITING, self.clock() + delay, f"retrying {step.step_id}") if delay else None

    def _latest(self, run: Any, step: StepSpec) -> StepAttempt:
        return self.store.latest_attempts(run.run_id)[step.step_id]

    def _set(self, run: Any, attempt: StepAttempt, state: StepState, detail: str, output: Any = None) -> None:
        try:
            self.store.set_step_state(run.run_id, attempt.step_id, attempt.attempt, state, detail=detail,
                                      output=output)
        except (ConflictError, IllegalTransition) as exc:
            raise _Abandon from exc

    def _quarantine(self, recipe: RecipeSpec, reason: str) -> None:
        with contextlib.suppress(IllegalTransition, ConflictError):
            self.store.transition(recipe.digest, RecipeState.QUARANTINED, reason=reason)

    def _finish(self, recipe: RecipeSpec, run: Any) -> None:
        attempts = self.store.latest_attempts(run.run_id)
        by_state: dict[StepState, list[str]] = {}
        for step in recipe.steps:
            by_state.setdefault(attempts[step.step_id].state, []).append(step.step_id)
        failed = by_state.get(StepState.FAILED, []) + [
            s for s in by_state.get(StepState.BLOCKED, []) if not attempts[s].detail.startswith(_DEPENDENCY_BLOCK)]
        if failed:
            self.store.set_run_state(run.run_id, RunState.FAILED, detail="did not complete: " + ", ".join(failed))
            return
        if run.mode is Mode.SHADOW:
            simulated = set(by_state.get(StepState.SIMULATED, []))
            simulated_writes = sum(1 for s in recipe.steps if s.step_id in simulated and s.is_side_effect)
            later = len(simulated) - simulated_writes
            self.store.set_run_state(run.run_id, RunState.UNVERIFIED, detail=(
                f"shadow run: {simulated_writes} write(s) simulated, nothing written"
                + (f"; {later} later step(s) not run because they follow a simulated write" if later else "")))
            return
        writes = [s.step_id for s in recipe.steps if s.is_side_effect and attempts[s.step_id].state in _HAS_OUTPUT]
        unverified = [s for s in writes if attempts[s].state is not StepState.OBSERVED_VERIFIED]
        if writes and not unverified and not by_state.get(StepState.BLOCKED):
            self.store.set_run_state(run.run_id, RunState.VERIFIED,
                                     detail="every write was independently observed: " + ", ".join(writes))
        elif not writes:
            self.store.set_run_state(run.run_id, RunState.UNVERIFIED, detail="no external effect was needed")
        else:
            self.store.set_run_state(run.run_id, RunState.UNVERIFIED,
                                     detail="not independently confirmed: " + ", ".join(
                                         unverified + by_state.get(StepState.BLOCKED, [])))


def _dedupe(payload: Mapping[str, Any], path: tuple[str | int, ...]) -> str | None:
    value = resolve_ref(Ref(root="trigger", step_id=None, path=path), {"trigger": payload})
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return str(value) or None
