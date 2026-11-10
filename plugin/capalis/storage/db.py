"""Persistent store: Recipe versions, lifecycle state, grants, runs, write intents and the event chain.

Every state change goes through the pure lifecycle tables and is applied as a compare-and-set, so two
concurrent writers cannot both move the same Recipe, run or step. Payloads are redacted before they are
written, and the event log is hash-chained to make accidental or naive edits detectable (it is not a
defence against someone who can rewrite the whole file).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ..core.canonical import canonical_json
from ..core.ids import sender_fingerprint
from ..core.lifecycle import (
    RecipeState,
    RunState,
    StepState,
    recipe_transition,
    run_transition,
    step_transition,
)
from ..core.model import ApprovalGrant, Effect, RecipeSpec, Target
from ..core.policy import Mode
from ..core.redact import Redactor
from ..core.verification import ContractVerdict, Observation
from ..core.wire import grant_from_wire, recipe_from_wire

GENESIS = "0" * 64

MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE recipes (
        digest TEXT PRIMARY KEY,
        recipe_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        wire TEXT NOT NULL,
        state TEXT NOT NULL,
        state_reason TEXT NOT NULL DEFAULT '',
        previous_digest TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE (recipe_id, version)
    );
    CREATE TABLE grants (
        grant_id TEXT PRIMARY KEY,
        recipe_digest TEXT NOT NULL REFERENCES recipes(digest),
        wire TEXT NOT NULL,
        issued_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        revoked_at INTEGER
    );
    CREATE INDEX grants_by_recipe ON grants (recipe_digest);
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        recipe_digest TEXT NOT NULL REFERENCES recipes(digest),
        recipe_id TEXT NOT NULL,
        mode TEXT NOT NULL,
        trigger_source TEXT NOT NULL,
        sender TEXT NOT NULL,
        dedupe_value TEXT NOT NULL,
        cause_id TEXT,
        payload TEXT NOT NULL,
        state TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        resume_at INTEGER,
        lease_owner TEXT,
        lease_until INTEGER NOT NULL DEFAULT 0,
        started_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE (recipe_id, mode, dedupe_value)
    );
    CREATE INDEX runs_by_state ON runs (state, resume_at);
    CREATE TABLE step_attempts (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        step_id TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        idempotency_key TEXT NOT NULL,
        effect TEXT NOT NULL,
        target TEXT,
        state TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        output TEXT,
        recorded_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (run_id, step_id, attempt)
    );
    CREATE TABLE confirmations (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        step_id TEXT NOT NULL,
        actor TEXT NOT NULL,
        at INTEGER NOT NULL,
        PRIMARY KEY (run_id, step_id)
    );
    CREATE TABLE observations (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        step_id TEXT NOT NULL,
        contract_id TEXT NOT NULL,
        probe_id TEXT NOT NULL,
        target TEXT,
        level TEXT NOT NULL,
        observed_at INTEGER NOT NULL,
        status TEXT NOT NULL,
        detail TEXT NOT NULL,
        observed_values TEXT NOT NULL,
        error TEXT
    );
    CREATE INDEX observations_by_run ON observations (run_id);
    CREATE TABLE controls (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL);
    CREATE TABLE preferences (actor TEXT PRIMARY KEY, presentation TEXT NOT NULL, updated_at INTEGER NOT NULL);
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        at INTEGER NOT NULL,
        kind TEXT NOT NULL,
        run_id TEXT,
        payload TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        hash TEXT NOT NULL
    );
    CREATE INDEX events_by_run ON events (run_id);
    """,
    # 2: display names for ids (channels, repositories, people), shown to people next to the id.
    """
    CREATE TABLE names (key TEXT PRIMARY KEY, label TEXT NOT NULL, updated_at INTEGER NOT NULL);
    """,
    # 3: capability contracts for other Hermes tools: proposed -> tested -> approved (or revoked).
    """
    CREATE TABLE contracts (
        capability_id TEXT PRIMARY KEY,
        spec TEXT NOT NULL,
        digest TEXT NOT NULL,
        state TEXT NOT NULL,
        test TEXT,
        actor TEXT,
        updated_at INTEGER NOT NULL
    );
    """,
)

PRESENTATIONS = frozenset({"recommend", "compare", "ask"})


class StoreError(RuntimeError):
    pass


class ConflictError(StoreError):
    """Another writer changed the row first; the caller must re-read and decide again."""


class RateLimited(StoreError):
    pass


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    recipe_digest: str
    recipe_id: str
    mode: Mode
    trigger_source: str
    dedupe_value: str
    cause_id: str | None
    payload: dict[str, Any]
    state: RunState
    detail: str
    resume_at: int | None
    started_at: int


@dataclass(frozen=True, slots=True)
class StepAttempt:
    run_id: str
    step_id: str
    attempt: int
    idempotency_key: str
    effect: Effect
    target: str | None
    state: StepState
    detail: str
    output: Any
    recorded_at: int


_RUN_COLUMNS = ("run_id, recipe_digest, recipe_id, mode, trigger_source, dedupe_value, cause_id, payload, state, "
                "detail, resume_at, started_at")
_ATTEMPT_COLUMNS = "run_id, step_id, attempt, idempotency_key, effect, target, state, detail, output, recorded_at"
_DISPATCHED_OR_LATER = frozenset(StepState) - {StepState.PLANNED, StepState.SKIPPED, StepState.BLOCKED,
                                               StepState.INTENT_RECORDED}


def _run_record(r: tuple[Any, ...]) -> RunRecord:
    return RunRecord(r[0], r[1], r[2], Mode(r[3]), r[4], r[5], r[6], json.loads(r[7]), RunState(r[8]), r[9], r[10],
                     r[11])


def _attempt(r: tuple[Any, ...]) -> StepAttempt:
    return StepAttempt(r[0], r[1], r[2], r[3], Effect(r[4]), r[5], StepState(r[6]), r[7],
                       None if r[8] is None else json.loads(r[8]), r[9])


@dataclass(frozen=True, slots=True)
class EventRecord:
    seq: int
    at: int
    kind: str
    run_id: str | None
    payload: dict[str, Any]
    prev_hash: str
    hash: str


def _event_hash(prev_hash: str, seq: int, at: int, kind: str, run_id: str | None, payload: str) -> str:
    body = canonical_json({"seq": seq, "at": at, "kind": kind, "run_id": run_id, "payload": payload})
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


class Store:
    def __init__(self, conn: sqlite3.Connection, *, clock: Callable[[], int] | None = None,
                 redactor: Redactor | None = None) -> None:
        # plugin_db hands out check_same_thread=False connections and leaves transactions to the caller:
        # take explicit control (no implicit BEGIN) and serialise this connection's users with a lock.
        conn.isolation_level = None
        self.conn = conn
        self.clock = clock or (lambda: int(time.time()))
        self.redactor = redactor or Redactor()
        self._lock = threading.RLock()
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.migrate()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        # BEGIN IMMEDIATE takes the write lock up front, so check-then-write sequences cannot interleave
        # with another connection's writes.
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def _read(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            return list(self.conn.execute(sql, params))

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
        rows = self._read(sql, params)
        return rows[0] if rows else None

    def migrate(self) -> int:
        with self._lock:
            self.conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                              "(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)")
        for version, script in enumerate(MIGRATIONS, start=1):
            with self._tx() as tx:
                # Re-checked under the write lock: another process may have migrated meanwhile.
                if tx.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,)).fetchone():
                    continue
                for statement in filter(str.strip, script.split(";")):
                    tx.execute(statement)
                tx.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, self.clock()))
        return len(MIGRATIONS)

    # -- Recipes ------------------------------------------------------------------------------------

    def save_recipe(self, recipe: RecipeSpec, state: RecipeState = RecipeState.COMPILED) -> bool:
        """Store an immutable Recipe version; False if this exact digest is already stored."""
        now = self.clock()
        with self._tx() as tx:
            existing = tx.execute("SELECT digest FROM recipes WHERE recipe_id = ? AND version = ?",
                                  (recipe.recipe_id, recipe.version)).fetchone()
            if existing is not None:
                if existing[0] != recipe.digest:
                    raise StoreError(f"{recipe.recipe_id} v{recipe.version} already exists with a different digest")
                return False
            tx.execute("INSERT INTO recipes (digest, recipe_id, version, wire, state, previous_digest, created_at, "
                       "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (recipe.digest, recipe.recipe_id, recipe.version, canonical_json(recipe.to_wire()),
                        state.value, recipe.previous_digest, now, now))
            self._event(tx, "recipe.saved", {"digest": recipe.digest, "recipe_id": recipe.recipe_id,
                                             "version": recipe.version, "state": state.value})
        return True

    def recipe(self, digest: str) -> RecipeSpec:
        row = self._one("SELECT wire FROM recipes WHERE digest = ?", (digest,))
        if row is None:
            raise StoreError(f"no stored Recipe {digest}")
        return recipe_from_wire(json.loads(row[0]), expected_digest=digest)

    def recipe_state(self, digest: str) -> RecipeState:
        row = self._one("SELECT state FROM recipes WHERE digest = ?", (digest,))
        if row is None:
            raise StoreError(f"no stored Recipe {digest}")
        return RecipeState(row[0])

    def latest(self, recipe_id: str) -> tuple[RecipeSpec, RecipeState] | None:
        row = self._one("SELECT digest, state FROM recipes WHERE recipe_id = ? ORDER BY version DESC LIMIT 1",
                        (recipe_id,))
        return None if row is None else (self.recipe(row[0]), RecipeState(row[1]))

    def version_in_state(self, recipe_id: str, state: RecipeState) -> RecipeSpec | None:
        row = self._one("SELECT digest FROM recipes WHERE recipe_id = ? AND state = ? ORDER BY version DESC LIMIT 1",
                        (recipe_id, state.value))
        return None if row is None else self.recipe(row[0])

    def recipes(self) -> list[tuple[str, int, str, RecipeState]]:
        rows = self._read("SELECT recipe_id, version, digest, state FROM recipes ORDER BY recipe_id, version")
        return [(r[0], r[1], r[2], RecipeState(r[3])) for r in rows]

    def transition(self, digest: str, target: RecipeState, *, reason: str = "") -> RecipeState:
        with self._tx() as tx:
            row = tx.execute("SELECT state FROM recipes WHERE digest = ?", (digest,)).fetchone()
            if row is None:
                raise StoreError(f"no stored Recipe {digest}")
            current = RecipeState(row[0])
            recipe_transition(current, target)
            updated = tx.execute("UPDATE recipes SET state = ?, state_reason = ?, updated_at = ? "
                                 "WHERE digest = ? AND state = ?",
                                 (target.value, reason, self.clock(), digest, current.value))
            if updated.rowcount != 1:
                raise ConflictError(f"Recipe {digest} changed state concurrently")
            self._event(tx, "recipe.state", {"digest": digest, "from": current.value, "to": target.value,
                                             "reason": reason})
        return target

    # -- Grants -------------------------------------------------------------------------------------

    def save_grant(self, grant: ApprovalGrant) -> None:
        with self._tx() as tx:
            row = tx.execute("SELECT recipe_id FROM recipes WHERE digest = ?", (grant.recipe_digest,)).fetchone()
            if row is None or row[0] != grant.recipe_id:
                raise StoreError("grant does not name a stored Recipe version")
            tx.execute("INSERT INTO grants VALUES (?, ?, ?, ?, ?, ?)",
                       (grant.grant_id, grant.recipe_digest, canonical_json(grant.to_wire()), grant.issued_at,
                        grant.expires_at, grant.revoked_at))
            self._event(tx, "grant.saved", {"grant_id": grant.grant_id, "recipe_digest": grant.recipe_digest,
                                            "actor": grant.actor, "expires_at": grant.expires_at})

    def revoke_grant(self, grant_id: str, *, actor: str) -> bool:
        now = self.clock()
        with self._tx() as tx:
            updated = tx.execute("UPDATE grants SET revoked_at = ? WHERE grant_id = ? AND revoked_at IS NULL",
                                 (now, grant_id))
            if updated.rowcount:
                self._event(tx, "grant.revoked", {"grant_id": grant_id, "actor": actor})
        return bool(updated.rowcount)

    def active_grant(self, recipe_digest: str, now: int | None = None) -> ApprovalGrant | None:
        at = self.clock() if now is None else now
        row = self._one(
            "SELECT wire, revoked_at FROM grants WHERE recipe_digest = ? AND revoked_at IS NULL AND issued_at <= ? "
            "AND expires_at > ? ORDER BY issued_at DESC LIMIT 1", (recipe_digest, at, at))
        if row is None:
            return None
        wire = json.loads(row[0])
        wire["revoked_at"] = row[1]
        return grant_from_wire(wire)

    # -- Controls and preferences -------------------------------------------------------------------

    def set_global_pause(self, paused: bool, *, actor: str, reason: str = "") -> None:
        with self._tx() as tx:
            tx.execute("INSERT INTO controls VALUES ('global_pause', ?, ?) ON CONFLICT(key) DO UPDATE SET "
                       "value = excluded.value, updated_at = excluded.updated_at",
                       ("1" if paused else "0", self.clock()))
            self._event(tx, "control.global_pause", {"paused": paused, "actor": actor, "reason": reason})

    def control(self, key: str) -> str | None:
        row = self._one("SELECT value FROM controls WHERE key = ?", (key,))
        return None if row is None else str(row[0])

    def set_control(self, key: str, value: str) -> None:
        with self._tx() as tx:
            tx.execute("INSERT INTO controls VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                       "updated_at = excluded.updated_at", (key, value, self.clock()))

    def last_event_seq(self) -> int:
        row = self._one("SELECT MAX(seq) FROM events")
        return int(row[0]) if row and row[0] is not None else 0

    def events_after(self, seq: int, limit: int = 200) -> list[EventRecord]:
        rows = self._read("SELECT seq, at, kind, run_id, payload, prev_hash, hash FROM events WHERE seq > ? "
                          "ORDER BY seq LIMIT ?", (seq, limit))
        return [EventRecord(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5], r[6]) for r in rows]

    def global_paused(self) -> bool:
        row = self._one("SELECT value FROM controls WHERE key = 'global_pause'")
        return row is not None and row[0] == "1"

    def set_presentation(self, actor: str, presentation: str) -> None:
        if presentation not in PRESENTATIONS:
            raise StoreError(f"presentation must be one of {sorted(PRESENTATIONS)}")
        with self._tx() as tx:
            tx.execute("INSERT INTO preferences VALUES (?, ?, ?) ON CONFLICT(actor) DO UPDATE SET "
                       "presentation = excluded.presentation, updated_at = excluded.updated_at",
                       (actor, presentation, self.clock()))

    def presentation(self, actor: str) -> str:
        row = self._one("SELECT presentation FROM preferences WHERE actor = ?", (actor,))
        return row[0] if row else "ask"

    def set_names(self, labels: Mapping[str, str]) -> None:
        if not labels:
            return
        now = self.clock()
        with self._tx() as tx:
            for key, label in labels.items():
                tx.execute("INSERT INTO names VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET label = excluded.label, "
                           "updated_at = excluded.updated_at", (key, self.redactor.text(label), now))

    def save_contract(self, capability_id: str, spec: Mapping[str, Any], digest: str) -> None:
        """A new or changed contract always starts over as proposed: approval never survives a change."""
        with self._tx() as tx:
            tx.execute("INSERT INTO contracts VALUES (?, ?, ?, 'proposed', NULL, NULL, ?) ON CONFLICT(capability_id) "
                       "DO UPDATE SET spec = excluded.spec, digest = excluded.digest, state = 'proposed', test = NULL, "
                       "actor = NULL, updated_at = excluded.updated_at WHERE contracts.digest != excluded.digest "
                       "OR contracts.state = 'revoked'",
                       (capability_id, canonical_json(spec), digest, self.clock()))
            self._event(tx, "contract.proposed", {"capability_id": capability_id, "digest": digest})

    def set_contract_state(self, capability_id: str, state: str, *, actor: str, test: Any = None) -> None:
        with self._tx() as tx:
            updated = tx.execute("UPDATE contracts SET state = ?, actor = ?, test = COALESCE(?, test), updated_at = ? "
                                 "WHERE capability_id = ?",
                                 (state, actor, None if test is None else canonical_json(self.redactor.value(test)),
                                  self.clock(), capability_id))
            if updated.rowcount != 1:
                raise StoreError(f"no contract {capability_id}")
            self._event(tx, f"contract.{state}", {"capability_id": capability_id, "actor": actor})

    def contracts(self) -> list[dict[str, Any]]:
        rows = self._read("SELECT capability_id, spec, digest, state, test, actor FROM contracts "
                          "ORDER BY capability_id")
        return [{"capability_id": r[0], "spec": json.loads(r[1]), "digest": r[2], "state": r[3],
                 "test": None if r[4] is None else json.loads(r[4]), "actor": r[5]} for r in rows]

    def names(self) -> dict[str, str]:
        return {key: label for key, label in self._read("SELECT key, label FROM names")}

    # -- Runs and step attempts ---------------------------------------------------------------------

    def start_run(self, recipe: RecipeSpec, *, mode: Mode, trigger_source: str, sender: str, dedupe_value: str,
                  payload: Mapping[str, Any], cause_id: str | None = None,
                  limits: tuple[int, int] | None = None) -> tuple[RunRecord, bool]:
        """Create a run, or return the existing one for a duplicate delivery of the same trigger.

        ``limits`` (per hour, per rolling day) is re-checked inside the write transaction, so concurrent
        triggers cannot both squeeze under the cap. The stored payload is redacted and is the only copy
        later steps read from.
        """
        now = self.clock()
        with self._tx() as tx:
            existing = tx.execute("SELECT run_id FROM runs WHERE recipe_id = ? AND mode = ? AND dedupe_value = ?",
                                  (recipe.recipe_id, mode.value, dedupe_value)).fetchone()
            if existing is not None:
                self._event(tx, "run.duplicate", {"dedupe_value": dedupe_value}, run_id=existing[0])
                run_id, created = existing[0], False
            else:
                if limits is not None:
                    for window, cap, code in ((3600, limits[0], "hourly"), (86400, limits[1], "daily")):
                        (count,) = tx.execute("SELECT COUNT(*) FROM runs WHERE recipe_id = ? AND mode = ? "
                                              "AND started_at > ?", (recipe.recipe_id, mode.value, now - window)
                                              ).fetchone()
                        if count >= cap:
                            raise RateLimited(f"the {code} run limit ({cap}) is reached")
                run_id, created = uuid.uuid4().hex, True
                tx.execute(f"INSERT INTO runs ({_RUN_COLUMNS}, sender, updated_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', NULL, ?, ?, ?)",
                           (run_id, recipe.digest, recipe.recipe_id, mode.value, trigger_source, dedupe_value,
                            cause_id, canonical_json(self.redactor.value(dict(payload))), RunState.QUEUED.value,
                            now, sender_fingerprint(trigger_source, sender), now))
                self._event(tx, "run.started", {"recipe_digest": recipe.digest, "mode": mode.value,
                                                "trigger_source": trigger_source, "cause_id": cause_id},
                            run_id=run_id)
        return self.run(run_id), created

    def run(self, run_id: str) -> RunRecord:
        row = self._one(f"SELECT {_RUN_COLUMNS} FROM runs WHERE run_id = ?", (run_id,))
        if row is None:
            raise StoreError(f"no run {run_id}")
        return _run_record(row)

    def runs(self, recipe_id: str | None = None, limit: int = 50) -> list[RunRecord]:
        where, params = ("WHERE recipe_id = ?", (recipe_id,)) if recipe_id else ("", ())
        rows = self._read(f"SELECT {_RUN_COLUMNS} FROM runs {where} ORDER BY started_at DESC, rowid DESC LIMIT ?",
                          (*params, limit))
        return [_run_record(r) for r in rows]

    def runs_since(self, recipe_id: str, since: int, mode: Mode = Mode.LIVE) -> int:
        row = self._one("SELECT COUNT(*) FROM runs WHERE recipe_id = ? AND mode = ? AND started_at >= ?",
                        (recipe_id, mode.value, since))
        return int(row[0]) if row else 0

    def set_run_state(self, run_id: str, target: RunState, *, detail: str = "",
                      resume_at: int | None = None) -> RunState:
        with self._tx() as tx:
            row = tx.execute("SELECT state FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise StoreError(f"no run {run_id}")
            current = RunState(row[0])
            if current is not target:
                run_transition(current, target)
            safe_detail = self.redactor.text(detail)
            updated = tx.execute("UPDATE runs SET state = ?, detail = ?, resume_at = ?, updated_at = ? "
                                 "WHERE run_id = ? AND state = ?",
                                 (target.value, safe_detail, resume_at, self.clock(), run_id, current.value))
            if updated.rowcount != 1:
                raise ConflictError(f"run {run_id} changed state concurrently")
            self._event(tx, "run.state", {"from": current.value, "to": target.value, "detail": safe_detail,
                                          "resume_at": resume_at}, run_id=run_id)
        return target

    def claim_run(self, run_id: str, owner: str, ttl_s: int) -> bool:
        """Lease a run to one executor; a crashed executor's lease simply expires."""
        now = self.clock()
        with self._tx() as tx:
            updated = tx.execute("UPDATE runs SET lease_owner = ?, lease_until = ? WHERE run_id = ? "
                                 "AND (lease_until <= ? OR lease_owner = ?)", (owner, now + ttl_s, run_id, now, owner))
        return updated.rowcount == 1

    def release_run(self, run_id: str, owner: str) -> None:
        with self._tx() as tx:
            tx.execute("UPDATE runs SET lease_owner = NULL, lease_until = 0 WHERE run_id = ? AND lease_owner = ?",
                       (run_id, owner))

    def due_runs(self, now: int | None = None) -> list[str]:
        """Runs an executor should pick up: parked runs whose time has come and runs whose executor died."""
        at = self.clock() if now is None else now
        rows = self._read(
            "SELECT run_id FROM runs WHERE lease_until <= ? "
            "AND (state IN (?, ?) OR (state IN (?, ?) AND resume_at <= ?)) "
            "ORDER BY COALESCE(resume_at, started_at), rowid",
            (at, RunState.QUEUED.value, RunState.RUNNING.value, RunState.WAITING.value,
             RunState.NEEDS_RECONCILIATION.value, at))
        return [r[0] for r in rows]

    def record_attempt(self, run_id: str, step_id: str, attempt: int, *, idempotency_key: str, effect: Effect,
                       target: Target | None, state: StepState = StepState.INTENT_RECORDED,
                       detail: str = "", output: Any = None) -> bool:
        """Durably record a step attempt (for side effects: the write intent); False if already recorded."""
        now = self.clock()
        safe_detail = self.redactor.text(detail)
        with self._tx() as tx:
            stored = None if output is None else canonical_json(self.redactor.value(output))
            try:
                tx.execute(f"INSERT INTO step_attempts ({_ATTEMPT_COLUMNS}, updated_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (run_id, step_id, attempt, idempotency_key, effect.value,
                            None if target is None else str(target), state.value, safe_detail, stored, now, now))
            except sqlite3.IntegrityError:
                return False
            self._event(tx, "step.intent", {"step_id": step_id, "attempt": attempt, "effect": effect.value,
                                            "target": None if target is None else str(target),
                                            "idempotency_key": idempotency_key, "state": state.value,
                                            "detail": safe_detail}, run_id=run_id)
        return True

    def set_step_state(self, run_id: str, step_id: str, attempt: int, target: StepState, *,
                       detail: str = "", output: Any = None) -> StepState:
        with self._tx() as tx:
            row = tx.execute("SELECT state FROM step_attempts WHERE run_id = ? AND step_id = ? AND attempt = ?",
                             (run_id, step_id, attempt)).fetchone()
            if row is None:
                raise StoreError(f"no attempt {run_id}/{step_id}#{attempt}")
            current = StepState(row[0])
            step_transition(current, target)
            safe_detail = self.redactor.text(detail)
            stored = None if output is None else canonical_json(self.redactor.value(output))
            updated = tx.execute("UPDATE step_attempts SET state = ?, detail = ?, output = COALESCE(?, output), "
                                 "updated_at = ? WHERE run_id = ? AND step_id = ? AND attempt = ? AND state = ?",
                                 (target.value, safe_detail, stored, self.clock(), run_id, step_id, attempt,
                                  current.value))
            if updated.rowcount != 1:
                raise ConflictError(f"step {run_id}/{step_id}#{attempt} changed state concurrently")
            self._event(tx, "step.state", {"step_id": step_id, "attempt": attempt, "from": current.value,
                                           "to": target.value, "detail": safe_detail}, run_id=run_id)
        return target

    def attempts(self, run_id: str) -> list[StepAttempt]:
        rows = self._read(f"SELECT {_ATTEMPT_COLUMNS} FROM step_attempts WHERE run_id = ? ORDER BY rowid", (run_id,))
        return [_attempt(r) for r in rows]

    def latest_attempts(self, run_id: str) -> dict[str, StepAttempt]:
        latest: dict[str, StepAttempt] = {}
        for attempt in self.attempts(run_id):
            if attempt.step_id not in latest or attempt.attempt > latest[attempt.step_id].attempt:
                latest[attempt.step_id] = attempt
        return latest

    def writes_dispatched(self, run_id: str) -> int:
        return sum(1 for a in self.attempts(run_id) if a.effect is not Effect.READ and a.state in _DISPATCHED_OR_LATER)

    def unfinished_intents(self) -> list[StepAttempt]:
        """Side-effect attempts a crash may have left mid-flight; recovery must resolve them before anything else."""
        rows = self._read(
            f"SELECT {_ATTEMPT_COLUMNS} FROM step_attempts WHERE effect != ? AND state IN (?, ?, ?) ORDER BY rowid",
            (Effect.READ.value, StepState.INTENT_RECORDED.value, StepState.DISPATCHED.value,
             StepState.UNKNOWN_EFFECT.value))
        return [_attempt(r) for r in rows]

    def confirm_step(self, run_id: str, step_id: str, *, actor: str) -> bool:
        with self._tx() as tx:
            try:
                tx.execute("INSERT INTO confirmations VALUES (?, ?, ?, ?)", (run_id, step_id, actor, self.clock()))
            except sqlite3.IntegrityError:
                return False
            self._event(tx, "step.confirmed", {"step_id": step_id, "actor": actor}, run_id=run_id)
        return True

    def confirmations(self, run_id: str) -> frozenset[str]:
        return frozenset(r[0] for r in self._read("SELECT step_id FROM confirmations WHERE run_id = ?", (run_id,)))

    def record_observation(self, run_id: str, step_id: str, observation: Observation,
                           verdict: ContractVerdict) -> None:
        with self._tx() as tx:
            values = canonical_json(self.redactor.value(dict(observation.values)))
            error = None if observation.error is None else self.redactor.text(observation.error)
            detail = self.redactor.text(verdict.detail)
            target = None if observation.target is None else str(observation.target)
            tx.execute("INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (run_id, step_id, observation.contract_id, observation.probe_id, target,
                        observation.level.value, observation.observed_at, verdict.status.value, detail, values, error))
            self._event(tx, "verify.observed", {"step_id": step_id, "contract_id": observation.contract_id,
                                                "probe_id": observation.probe_id, "target": target,
                                                "level": observation.level.value, "status": verdict.status.value,
                                                "detail": detail}, run_id=run_id)

    def observations(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._read("SELECT step_id, contract_id, probe_id, target, level, observed_at, status, detail, "
                          "observed_values, error FROM observations WHERE run_id = ? ORDER BY rowid", (run_id,))
        return [{"step_id": r[0], "contract_id": r[1], "probe_id": r[2], "target": r[3], "level": r[4],
                 "observed_at": r[5], "status": r[6], "detail": r[7], "values": json.loads(r[8]), "error": r[9]}
                for r in rows]

    # -- Events -------------------------------------------------------------------------------------

    def _event(self, tx: sqlite3.Connection, kind: str, payload: dict[str, Any], *, run_id: str | None = None) -> str:
        row = tx.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev = (row[0] + 1, row[1]) if row else (1, GENESIS)
        at = self.clock()
        body = canonical_json(self.redactor.value(payload))
        digest = _event_hash(prev, seq, at, kind, run_id, body)
        tx.execute("INSERT INTO events (seq, at, kind, run_id, payload, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (seq, at, kind, run_id, body, prev, digest))
        return digest

    def append_event(self, kind: str, payload: dict[str, Any], *, run_id: str | None = None) -> str:
        with self._tx() as tx:
            return self._event(tx, kind, payload, run_id=run_id)

    def events(self, run_id: str | None = None) -> list[EventRecord]:
        query = "SELECT seq, at, kind, run_id, payload, prev_hash, hash FROM events"
        rows = self._read(query + (" WHERE run_id = ? ORDER BY seq" if run_id else " ORDER BY seq"),
                          (run_id,) if run_id else ())
        return [EventRecord(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5], r[6]) for r in rows]

    def verify_chain(self) -> tuple[bool, int | None]:
        prev = GENESIS
        for seq, at, kind, run_id, payload, prev_hash, digest in self._read(
                "SELECT seq, at, kind, run_id, payload, prev_hash, hash FROM events ORDER BY seq"):
            if prev_hash != prev or _event_hash(prev, seq, at, kind, run_id, payload) != digest:
                return False, seq
            prev = digest
        return True, None
