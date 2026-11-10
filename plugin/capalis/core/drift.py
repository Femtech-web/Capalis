from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from .model import TRUSTED_LEVELS, Availability, CapabilityDescriptor, DependencyPin, RecipeSpec


class DriftKind(StrEnum):
    MISSING = "missing"
    SCHEMA_CHANGED = "schema_changed"
    VERSION_CHANGED = "version_changed"
    PROVIDER_CHANGED = "provider_changed"
    TRUST_LOWERED = "trust_lowered"
    AUTH_CHANGED = "auth_changed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DriftChange:
    capability_id: str
    kind: DriftKind
    before: str
    after: str


@dataclass(frozen=True, slots=True)
class DriftReport:
    recipe_digest: str
    changes: tuple[DriftChange, ...]

    @property
    def blocking(self) -> bool:
        # D-015: every pin change is material until a finer, tested policy exists.
        return bool(self.changes)

    def plain(self) -> str:
        """The reason in words an owner understands; ``summary`` keeps the technical detail for evidence."""
        if not self.changes:
            return "nothing it depends on changed"
        versions = sorted({(c.before, c.after) for c in self.changes if c.kind is DriftKind.VERSION_CHANGED})
        if (len(versions) == 1 and all(c.capability_id.startswith("capalis:") for c in self.changes)
                and {c.kind for c in self.changes} <= {DriftKind.VERSION_CHANGED, DriftKind.SCHEMA_CHANGED}):
            before, after = versions[0]
            return f"Capalis was upgraded ({before} → {after}), so the tools this Recipe uses changed"

        def names(kind: DriftKind) -> str:
            found = sorted({c.capability_id.split(":", 1)[-1] for c in self.changes if c.kind is kind})
            return ", ".join(found[:3]) + (f" and {len(found) - 3} more" if len(found) > 3 else "")

        words = {
            DriftKind.MISSING: "{} is no longer available",
            DriftKind.UNAVAILABLE: "{} can't be used right now (a token or permission may be missing)",
            DriftKind.AUTH_CHANGED: "{} needs different credentials",
            DriftKind.TRUST_LOWERED: "{} is no longer trusted",
            DriftKind.PROVIDER_CHANGED: "{} now comes from a different provider",
            DriftKind.SCHEMA_CHANGED: "the shape of {} changed",
            DriftKind.VERSION_CHANGED: "{} changed version",
        }
        present = [kind for kind in words if any(c.kind is kind for c in self.changes)]
        return "; ".join(words[kind].format(names(kind)) for kind in present)

    def summary(self) -> str:
        if not self.changes:
            return "No dependency changes since the Recipe was compiled."
        return "; ".join(f"{change.capability_id}: {change.kind.value} ({change.before} -> {change.after})"
                         for change in self.changes)


def _compare(pin: DependencyPin, current: CapabilityDescriptor | None) -> list[DriftChange]:
    cid = pin.capability_id
    if current is None:
        return [DriftChange(cid, DriftKind.MISSING, pin.version, "absent")]
    changes = []
    if current.schema_fingerprint != pin.schema_fingerprint:
        changes.append(DriftChange(cid, DriftKind.SCHEMA_CHANGED, pin.schema_fingerprint[:19],
                                   current.schema_fingerprint[:19]))
    if current.version != pin.version:
        changes.append(DriftChange(cid, DriftKind.VERSION_CHANGED, pin.version, current.version))
    if current.provider != pin.provider:
        changes.append(DriftChange(cid, DriftKind.PROVIDER_CHANGED, pin.provider, current.provider))
    if pin.trust in TRUSTED_LEVELS and current.trust not in TRUSTED_LEVELS:
        changes.append(DriftChange(cid, DriftKind.TRUST_LOWERED, pin.trust.value, current.trust.value))
    if current.auth_requirement != pin.auth_requirement:
        changes.append(DriftChange(cid, DriftKind.AUTH_CHANGED, str(pin.auth_requirement),
                                   str(current.auth_requirement)))
    if current.availability is not Availability.AVAILABLE:
        changes.append(DriftChange(cid, DriftKind.UNAVAILABLE, Availability.AVAILABLE.value,
                                   current.availability.value))
    return changes


def detect_drift(recipe: RecipeSpec, current: Mapping[str, CapabilityDescriptor]) -> DriftReport:
    changes = [change for cid, pin in sorted(recipe.pins.items()) for change in _compare(pin, current.get(cid))]
    return DriftReport(recipe_digest=recipe.digest, changes=tuple(changes))
