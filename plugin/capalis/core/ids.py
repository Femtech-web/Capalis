from __future__ import annotations

import hashlib


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def idempotency_key(recipe_id: str, dedupe_value: str, step_id: str) -> str:
    """Stable across retries, restarts and duplicate deliveries of the same trigger.

    It deliberately excludes the attempt number and run id, so reconciliation after a crash looks up
    the effect of *any* earlier attempt for this trigger and step, not just the latest one.
    """
    return "cap-" + _hash("idem", recipe_id, dedupe_value, step_id)[:32]


def sender_fingerprint(source: str, sender: str) -> str:
    return _hash("sender", source, sender)[:24]
