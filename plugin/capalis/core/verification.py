"""Outcome contract evaluation (I-03, FR-VER-03..06).

An observation only counts when it came from the contract's own probe, against the exact compiled
target, recently enough, at the required evidence level. A tool's own success response (V0) can never
satisfy a contract, and human confirmation is recorded on a separate axis.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .expressions import MISSING, Ref, resolve_ref, resolve_value
from .model import EVIDENCE_RANK, EvidenceLevel, Expectation, OutcomeContract, Target


class ContractStatus(StrEnum):
    MET = "met"
    NOT_MET = "not_met"
    PENDING = "pending"
    MISSING = "missing"
    STALE = "stale"
    ERROR = "error"


class OutcomeStatus(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"
    PENDING = "pending"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class Observation:
    contract_id: str
    probe_id: str
    target: Target | None
    level: EvidenceLevel
    observed_at: int
    values: Mapping[str, Any]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ContractVerdict:
    contract_id: str
    status: ContractStatus
    level: EvidenceLevel | None
    detail: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class HumanConfirmation:
    actor: str
    statement: str
    at: int


@dataclass(frozen=True, slots=True)
class OutcomeVerdict:
    status: OutcomeStatus
    contracts: tuple[ContractVerdict, ...]
    human: tuple[HumanConfirmation, ...] = ()

    @property
    def human_confirmed(self) -> bool:
        return bool(self.human)


def _check(expectation: Expectation, observed: Mapping[str, Any], scope: Mapping[str, Any]) -> str | None:
    actual = resolve_ref_path(observed, expectation.path)
    if expectation.op == "exists":
        return None if actual is not MISSING and actual is not None else "field is absent"
    if actual is MISSING:
        return "field is absent"
    assert expectation.value is not None
    wanted = resolve_value(expectation.value, scope)
    if wanted is MISSING:
        return "the expected value could not be resolved"
    if expectation.op == "equals":
        return None if actual == wanted else "value differs"
    if expectation.op == "not_equals":
        return None if actual != wanted else "value matches when it must not"
    if expectation.op == "contains":
        if isinstance(actual, str) and isinstance(wanted, str):
            return None if wanted in actual else "text not found"
        if isinstance(actual, Sequence) and not isinstance(actual, str):
            return None if wanted in actual else "item not found"
        return "value cannot contain the expected item"
    if expectation.op == "in":
        if isinstance(wanted, Sequence) and not isinstance(wanted, str):
            return None if actual in wanted else "value is not among the expected ones"
        return "expected value is not a list"
    return f"unknown op {expectation.op!r}"


def resolve_ref_path(values: Mapping[str, Any], path: tuple[str | int, ...]) -> Any:
    return resolve_ref(Ref(root="steps", step_id="_", path=path), {"steps": {"_": values}})


def evaluate_contract(contract: OutcomeContract, observation: Observation | None, *, now: int,
                      started_at: int, scope: Mapping[str, Any]) -> ContractVerdict:
    def verdict(status: ContractStatus, detail: str, level: EvidenceLevel | None = None) -> ContractVerdict:
        return ContractVerdict(contract.contract_id, status, level, detail, contract.required)

    if observation is None:
        if now < started_at + contract.deadline_s:
            return verdict(ContractStatus.PENDING, "waiting for an independent observation")
        return verdict(ContractStatus.MISSING, "no independent observation before the deadline")
    if observation.contract_id != contract.contract_id or observation.probe_id != contract.probe:
        return verdict(ContractStatus.ERROR, "observation came from a different check than the contract names")
    if EVIDENCE_RANK[observation.level] < EVIDENCE_RANK[contract.level]:
        return verdict(ContractStatus.MISSING,
                       f"{observation.level.value} evidence does not meet the required {contract.level.value}")
    if contract.probe_target is not None and observation.target != contract.probe_target:
        return verdict(ContractStatus.ERROR, f"observed {observation.target}, expected {contract.probe_target}")
    if observation.error:
        return verdict(ContractStatus.ERROR, f"the probe failed: {observation.error}")
    if now - observation.observed_at > contract.freshness_s:
        return verdict(ContractStatus.STALE, "the observation is older than the contract allows")
    failures = [f"{'.'.join(map(str, item.path))}: {problem}" for item in contract.expect
                if (problem := _check(item, observation.values, scope)) is not None]
    if failures:
        return verdict(ContractStatus.NOT_MET, "; ".join(failures), observation.level)
    return verdict(ContractStatus.MET, "independently observed", contract.level)


def summarize(verdicts: Sequence[ContractVerdict],
              human: Sequence[HumanConfirmation] = ()) -> OutcomeVerdict:
    required = [v for v in verdicts if v.required]
    if any(v.status is ContractStatus.NOT_MET for v in required):
        status = OutcomeStatus.FAILED
    elif required and all(v.status is ContractStatus.MET for v in required):
        status = OutcomeStatus.VERIFIED
    elif any(v.status is ContractStatus.PENDING for v in required):
        status = OutcomeStatus.PENDING
    else:
        # No required contract, or some are missing/stale/errored: never report verified (FR-VER-06).
        status = OutcomeStatus.UNVERIFIED
    return OutcomeVerdict(status, tuple(verdicts), tuple(human))
