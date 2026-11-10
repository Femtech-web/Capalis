"""CapabilityMatch: compare candidate capabilities for each requirement of a goal.

Rankings use an explicit, explainable priority: can it run safely in a managed Recipe, can its outcome
be independently verified, can it be retried without duplicating effects, how much setup it needs, and
what Capalis has actually observed. Every factor carries its evidence basis; nothing is invented. When
the evidence does not separate the top candidates, no winner is recommended.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .canonical import digest
from .model import (
    EFFECT_RISK,
    TRUSTED_LEVELS,
    Availability,
    CapabilityDescriptor,
    Effect,
    EvidenceLevel,
    IdempotencySupport,
)


class Basis(StrEnum):
    DOCUMENTED = "documented"
    TESTED = "tested"
    OBSERVED = "observed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Requirement:
    need_id: str
    operation: str
    description: str
    max_effect: Effect
    min_evidence: EvidenceLevel | None = EvidenceLevel.V1


@dataclass(frozen=True, slots=True)
class ObservedStats:
    """What Capalis has actually seen this capability do, with the environment it was seen in."""

    verified: int = 0
    failed: int = 0
    unverified: int = 0
    environment: str = "none"


@dataclass(frozen=True, slots=True)
class Factor:
    name: str
    summary: str
    rank: int
    basis: Basis


@dataclass(frozen=True, slots=True)
class Assessment:
    capability_id: str
    executable: bool
    exclusions: tuple[str, ...]
    factors: tuple[Factor, ...]
    setup: str

    def factor(self, name: str) -> Factor:
        return next(f for f in self.factors if f.name == name)

    @property
    def key(self) -> tuple[int, ...]:
        return (int(self.executable), *(f.rank for f in self.factors))


@dataclass(frozen=True, slots=True)
class RequirementMatch:
    requirement: Requirement
    candidates: tuple[Assessment, ...]
    recommended: str | None
    rationale: str


@dataclass(frozen=True, slots=True)
class MatchDecision:
    goal_digest: str
    matches: tuple[RequirementMatch, ...]

    @property
    def recommended(self) -> dict[str, str] | None:
        """One capability per requirement, or None when any requirement lacks a supported winner."""
        chosen = {m.requirement.need_id: m.recommended for m in self.matches}
        return None if any(v is None for v in chosen.values()) else chosen  # type: ignore[return-value]

    @property
    def gaps(self) -> tuple[str, ...]:
        return tuple(m.requirement.need_id for m in self.matches if not any(c.executable for c in m.candidates))


FACTOR_ORDER = ("verification", "idempotency", "setup", "reliability")
_SETUP_RANK = {Availability.AVAILABLE: 3, Availability.NEEDS_AUTH: 2, Availability.DISABLED: 1,
               Availability.NOT_INSTALLED: 1, Availability.UNREACHABLE: 0, Availability.UNKNOWN: 0}


def _verification(cap: CapabilityDescriptor, registry: Mapping[str, CapabilityDescriptor]) -> Factor:
    if cap.effective_effect is Effect.READ:
        return Factor("verification", "read-only; nothing to verify", 2, Basis.DOCUMENTED)
    probes = [registry[p] for p in cap.readback if p in registry]
    usable = [p for p in probes if p.effective_effect is Effect.READ and p.trust in TRUSTED_LEVELS]
    if usable:
        ready = [p for p in usable if p.availability is Availability.AVAILABLE]
        state = "" if ready else " once its read access is set up"
        return Factor("verification", f"its result can be read back independently ({usable[0].id}){state}",
                      2, Basis.DOCUMENTED)
    return Factor("verification", "no independent read-back; outcomes stay unverified", 0, Basis.DOCUMENTED)


def _idempotency(cap: CapabilityDescriptor) -> Factor:
    if cap.effective_effect is Effect.READ:
        return Factor("idempotency", "read-only; safe to repeat", 2, Basis.DOCUMENTED)
    if cap.idempotency is IdempotencySupport.NATIVE_KEY:
        return Factor("idempotency", "provider idempotency keys prevent duplicates", 2, Basis.DOCUMENTED)
    if cap.idempotency is IdempotencySupport.RECONCILE:
        return Factor("idempotency", "duplicates are prevented by looking up earlier writes", 2, Basis.DOCUMENTED)
    return Factor("idempotency", "an interrupted write cannot be safely retried; it stops for review",
                  0, Basis.DOCUMENTED)


def _setup(cap: CapabilityDescriptor) -> Factor:
    rank = _SETUP_RANK[cap.availability]
    summary = {3: "ready now", 2: "needs a sign-in or token", 1: "needs installing or enabling",
               0: "currently unreachable or unknown"}[rank]
    return Factor("setup", summary, rank, Basis.OBSERVED if cap.availability is not Availability.UNKNOWN
                  else Basis.UNKNOWN)


def _reliability(stats: ObservedStats | None) -> Factor:
    if stats is None or stats.verified + stats.failed + stats.unverified == 0:
        return Factor("reliability", "no runs observed yet", 0, Basis.UNKNOWN)
    summary = (f"{stats.verified} verified, {stats.failed} failed, {stats.unverified} unverified "
               f"({stats.environment})")
    rank = 1 if stats.verified and not stats.failed else 0
    return Factor("reliability", summary, rank, Basis.OBSERVED)


def assess(cap: CapabilityDescriptor, requirement: Requirement, registry: Mapping[str, CapabilityDescriptor],
           stats: ObservedStats | None = None) -> Assessment:
    exclusions: list[str] = []
    effect = cap.effective_effect
    if cap.shell_like:
        exclusions.append("runs shell or arbitrary commands")
    if effect is Effect.UNKNOWN:
        exclusions.append("its effects are not characterized by a trusted contract test")
    elif EFFECT_RISK[effect] > EFFECT_RISK[requirement.max_effect]:
        exclusions.append(f"its {effect.value} effect exceeds what this step allows ({requirement.max_effect.value})")
    if cap.availability is Availability.UNREACHABLE:
        exclusions.append("it is currently unreachable")
    factors = (_verification(cap, registry), _idempotency(cap), _setup(cap), _reliability(stats))
    if requirement.min_evidence is not None and effect is not Effect.READ and factors[0].rank == 0:
        exclusions.append(f"this step requires {requirement.min_evidence.value} evidence, which it cannot provide")
    return Assessment(cap.id, not exclusions, tuple(exclusions), factors, cap.setup_hint)


def _rationale(match_list: Sequence[Assessment], recommended: Assessment | None, req: Requirement) -> str:
    executable = [a for a in match_list if a.executable]
    if not match_list:
        return f"No known capability performs '{req.operation}'."
    if not executable:
        reasons = "; ".join(f"{a.capability_id}: {a.exclusions[0]}" for a in match_list[:3])
        return f"No candidate can run safely for this step ({reasons})."
    if recommended is None:
        return ("The top candidates are equally supported by the available evidence; compare them or test both "
                "before choosing.")
    if len(executable) == 1:
        return f"{recommended.capability_id} is the only candidate that can run safely for this step."
    runner_up = executable[1]
    for name in FACTOR_ORDER:
        mine, theirs = recommended.factor(name), runner_up.factor(name)
        if mine.rank > theirs.rank:
            return (f"Recommended {recommended.capability_id} on {name}. {recommended.capability_id}: "
                    f"{mine.summary}. {runner_up.capability_id}: {theirs.summary}.")
    return f"Recommended {recommended.capability_id}."


def match_requirement(requirement: Requirement, registry: Mapping[str, CapabilityDescriptor],
                      observed: Mapping[str, ObservedStats] | None = None) -> RequirementMatch:
    observed = observed or {}
    candidates = [assess(cap, requirement, registry, observed.get(cap.id))
                  for cap in registry.values() if cap.operation == requirement.operation]
    ranked = sorted(candidates, key=lambda a: (tuple(-k for k in a.key), a.capability_id))
    executable = [a for a in ranked if a.executable]
    recommended = None
    if len(executable) == 1 or (len(executable) > 1 and executable[0].key > executable[1].key):
        recommended = executable[0]
    return RequirementMatch(requirement, tuple(ranked), recommended.capability_id if recommended else None,
                            _rationale(ranked, recommended, requirement))


def match_goal(requirements: Sequence[Requirement], registry: Mapping[str, CapabilityDescriptor],
               observed: Mapping[str, ObservedStats] | None = None) -> MatchDecision:
    goal = digest([{"need": r.need_id, "operation": r.operation, "max_effect": r.max_effect.value,
                    "min_evidence": r.min_evidence.value if r.min_evidence else None} for r in requirements])
    return MatchDecision(goal, tuple(match_requirement(r, registry, observed) for r in requirements))
