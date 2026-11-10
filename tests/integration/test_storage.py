from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from capalis.core.lifecycle import IllegalTransition, RecipeState, RunState, StepState
from capalis.core.model import Effect, EvidenceLevel, RecipeSpec, Target, grant_for
from capalis.core.policy import Mode
from capalis.core.redact import Redactor
from capalis.core.verification import ContractStatus, ContractVerdict, Observation
from capalis.storage.db import ConflictError, Store, StoreError
from support import REPO, compile_draft, hero_draft

NOW = 1_800_000_000
CANARY = "canary-secret-5d1e0c7b9a"
PAYLOAD = {"message_id": "m-1", "author": "rep", "text": "it broke"}


class Clock:
    def __init__(self, now: int = NOW) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def connect(path: Path) -> sqlite3.Connection:
    # Same shape as Hermes plugin_db: WAL, busy timeout, shared across threads.
    conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "capalis.db"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def open_store(db_path: Path, clock: Clock) -> Iterator[Callable[[], Store]]:
    conns: list[sqlite3.Connection] = []

    def factory() -> Store:
        conn = connect(db_path)
        conns.append(conn)
        return Store(conn, clock=clock, redactor=Redactor([CANARY]))

    yield factory
    for conn in conns:
        conn.close()


@pytest.fixture
def recipe() -> RecipeSpec:
    result = compile_draft(hero_draft())
    assert result.recipe is not None
    return result.recipe


def _to_live(store: Store, digest: str) -> None:
    for state in (RecipeState.TESTED, RecipeState.SHADOW_QUALIFIED, RecipeState.AWAITING_APPROVAL, RecipeState.LIVE):
        store.transition(digest, state)


def test_restart_reloads_the_exact_compiled_recipe(open_store: Callable[[], Store], recipe: RecipeSpec) -> None:
    store = open_store()
    assert store.save_recipe(recipe)
    _to_live(store, recipe.digest)
    store.set_global_pause(True, actor="owner", reason="maintenance")

    reopened = open_store()
    assert reopened.migrate() == 2
    assert reopened.recipe(recipe.digest) == recipe
    assert reopened.recipe_state(recipe.digest) is RecipeState.LIVE
    assert reopened.global_paused()
    latest = reopened.latest(recipe.recipe_id)
    assert latest is not None and latest[0].digest == recipe.digest


def test_recipe_versions_are_immutable(open_store: Callable[[], Store], recipe: RecipeSpec) -> None:
    store = open_store()
    assert store.save_recipe(recipe)
    assert not store.save_recipe(recipe)
    other = compile_draft(hero_draft() | {"intent": "a different intent"})
    assert other.recipe is not None and other.recipe.digest != recipe.digest
    with pytest.raises(StoreError, match="different digest"):
        store.save_recipe(other.recipe)


def test_tampered_stored_recipe_is_refused(open_store: Callable[[], Store], db_path: Path,
                                          recipe: RecipeSpec) -> None:
    store = open_store()
    store.save_recipe(recipe)
    store.conn.execute("UPDATE recipes SET wire = replace(wire, ?, ?)", (REPO, "attacker/repo"))
    with pytest.raises(ValueError, match="digest"):
        open_store().recipe(recipe.digest)


def test_lifecycle_is_enforced_in_storage(open_store: Callable[[], Store], recipe: RecipeSpec) -> None:
    store = open_store()
    store.save_recipe(recipe)
    with pytest.raises(IllegalTransition):
        store.transition(recipe.digest, RecipeState.LIVE)
    _to_live(store, recipe.digest)
    store.transition(recipe.digest, RecipeState.QUARANTINED, reason="schema drift")
    for blocked in (RecipeState.LIVE, RecipeState.PAUSED, RecipeState.AWAITING_APPROVAL):
        with pytest.raises(IllegalTransition):
            store.transition(recipe.digest, blocked)
    store.transition(recipe.digest, RecipeState.RETIRED)
    kinds = [(e.payload["from"], e.payload["to"]) for e in store.events() if e.kind == "recipe.state"]
    assert kinds[-2:] == [("live", "quarantined"), ("quarantined", "retired")]


def test_concurrent_activation_happens_once(open_store: Callable[[], Store], recipe: RecipeSpec) -> None:
    first = open_store()
    first.save_recipe(recipe)
    for state in (RecipeState.TESTED, RecipeState.SHADOW_QUALIFIED, RecipeState.AWAITING_APPROVAL):
        first.transition(recipe.digest, state)
    stores = [open_store() for _ in range(6)]
    outcomes: list[str] = []
    barrier = threading.Barrier(len(stores))

    def activate(store: Store) -> None:
        barrier.wait()
        try:
            store.transition(recipe.digest, RecipeState.LIVE)
            outcomes.append("ok")
        except (IllegalTransition, ConflictError):
            outcomes.append("refused")

    threads = [threading.Thread(target=activate, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["ok"] + ["refused"] * (len(stores) - 1)


def test_grants_expire_and_revoke(open_store: Callable[[], Store], clock: Clock, recipe: RecipeSpec) -> None:
    store = open_store()
    store.save_recipe(recipe)
    grant = grant_for(recipe, grant_id="g-1", actor="owner", now=NOW, ttl_s=3600)
    store.save_grant(grant)
    active = store.active_grant(recipe.digest)
    assert active == grant
    assert store.active_grant(recipe.digest, now=NOW + 3600) is None
    assert store.revoke_grant("g-1", actor="owner")
    assert not store.revoke_grant("g-1", actor="owner")
    assert open_store().active_grant(recipe.digest) is None


def test_grant_must_name_a_stored_version(open_store: Callable[[], Store], recipe: RecipeSpec) -> None:
    store = open_store()
    with pytest.raises(StoreError):
        store.save_grant(grant_for(recipe, grant_id="g-0", actor="owner", now=NOW))
    store.save_recipe(recipe)
    grant = grant_for(recipe, grant_id="g-1", actor="owner", now=NOW)
    with pytest.raises(StoreError):
        store.save_grant(replace(grant, recipe_id="some-other-recipe"))


def test_presentation_preference(open_store: Callable[[], Store]) -> None:
    store = open_store()
    assert store.presentation("owner") == "ask"
    store.set_presentation("owner", "recommend")
    assert open_store().presentation("owner") == "recommend"
    with pytest.raises(StoreError):
        store.set_presentation("owner", "always-yes")


def test_duplicate_trigger_delivery_reuses_the_run(open_store: Callable[[], Store], recipe: RecipeSpec) -> None:
    store = open_store()
    store.save_recipe(recipe)
    run, created = store.start_run(recipe, mode=Mode.LIVE, trigger_source="discord", sender="u1",
                                   dedupe_value="msg-1", payload=PAYLOAD)
    again, created_again = open_store().start_run(recipe, mode=Mode.LIVE, trigger_source="discord", sender="u1",
                                                  dedupe_value="msg-1", payload=PAYLOAD)
    assert created and not created_again and again.run_id == run.run_id
    shadow, shadow_created = store.start_run(recipe, mode=Mode.SHADOW, trigger_source="discord", sender="u1",
                                             dedupe_value="msg-1", payload=PAYLOAD)
    assert shadow_created and shadow.run_id != run.run_id
    assert run.state is RunState.QUEUED
    assert store.runs_since(recipe.recipe_id, NOW) == 1
    assert "u1" not in repr(store.events())


def test_concurrent_duplicate_deliveries_create_one_run(open_store: Callable[[], Store],
                                                        recipe: RecipeSpec) -> None:
    open_store().save_recipe(recipe)
    stores = [open_store() for _ in range(8)]
    results: list[tuple[str, bool]] = []
    barrier = threading.Barrier(len(stores))

    def deliver(store: Store) -> None:
        barrier.wait()
        run, created = store.start_run(recipe, mode=Mode.LIVE, trigger_source="discord", sender="u1",
                                       dedupe_value="msg-42", payload=PAYLOAD)
        results.append((run.run_id, created))

    threads = [threading.Thread(target=deliver, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({run_id for run_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1


def test_shared_connection_is_safe_across_threads(open_store: Callable[[], Store], recipe: RecipeSpec) -> None:
    store = open_store()
    store.save_recipe(recipe)
    errors: list[BaseException] = []

    def deliver(i: int) -> None:
        try:
            store.start_run(recipe, mode=Mode.LIVE, trigger_source="discord", sender="u", dedupe_value=f"m{i}",
                            payload=PAYLOAD)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=deliver, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert store.runs_since(recipe.recipe_id, NOW) == 20
    assert store.verify_chain() == (True, None)


def test_intents_survive_a_crash_for_reconciliation(open_store: Callable[[], Store], recipe: RecipeSpec) -> None:
    store = open_store()
    store.save_recipe(recipe)
    run, _ = store.start_run(recipe, mode=Mode.LIVE, trigger_source="discord", sender="u1",
                             dedupe_value="msg-1", payload=PAYLOAD)
    store.set_run_state(run.run_id, RunState.RUNNING)
    target = Target("github.repo", REPO)
    assert store.record_attempt(run.run_id, "create", 1, idempotency_key="cap-k", effect=Effect.EXTERNAL_WRITE,
                               target=target)
    assert not store.record_attempt(run.run_id, "create", 1, idempotency_key="cap-k", effect=Effect.EXTERNAL_WRITE,
                                   target=target)
    store.set_step_state(run.run_id, "create", 1, StepState.DISPATCHED)
    del store

    recovered = open_store()
    pending = recovered.unfinished_intents()
    assert [(i.run_id, i.step_id, i.attempt, i.state) for i in pending] == [
        (run.run_id, "create", 1, StepState.DISPATCHED)]
    assert pending[0].idempotency_key == "cap-k" and pending[0].target == str(target)
    recovered.set_step_state(run.run_id, "create", 1, StepState.UNKNOWN_EFFECT, detail="timeout after send")
    recovered.set_step_state(run.run_id, "create", 1, StepState.OBSERVED_VERIFIED, detail="found by key")
    assert recovered.unfinished_intents() == []
    with pytest.raises(IllegalTransition):
        recovered.set_step_state(run.run_id, "create", 1, StepState.DISPATCHED)


def test_failed_write_leaves_no_partial_state(open_store: Callable[[], Store], recipe: RecipeSpec,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    store = open_store()

    def boom(_: object) -> object:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(store.redactor, "value", boom)
    with pytest.raises(RuntimeError):
        store.save_recipe(recipe)
    monkeypatch.undo()
    assert store.recipes() == [] and store.events() == []
    assert store.save_recipe(recipe)


def test_event_chain_detects_edits(open_store: Callable[[], Store], recipe: RecipeSpec) -> None:
    store = open_store()
    store.save_recipe(recipe)
    _to_live(store, recipe.digest)
    assert store.verify_chain() == (True, None)
    store.conn.execute("UPDATE events SET payload = replace(payload, 'live', 'paused') WHERE seq = 5")
    assert store.verify_chain() == (False, 5)


def test_event_chain_detects_deleted_rows(open_store: Callable[[], Store]) -> None:
    store = open_store()
    for i in range(4):
        store.append_event("note", {"i": i})
    store.conn.execute("DELETE FROM events WHERE seq = 2")
    assert store.verify_chain() == (False, 3)


def test_canary_secret_never_reaches_disk(open_store: Callable[[], Store], db_path: Path,
                                          recipe: RecipeSpec) -> None:
    store = open_store()
    store.save_recipe(recipe)
    run, _ = store.start_run(recipe, mode=Mode.LIVE, trigger_source="discord", sender="u1",
                             dedupe_value="msg-1", payload=PAYLOAD)
    store.set_run_state(run.run_id, RunState.RUNNING, detail=f"auth {CANARY}")
    store.record_attempt(run.run_id, "notify", 1, idempotency_key="cap-n", effect=Effect.EXTERNAL_SEND,
                        target=Target("slack.channel", "C0ABCDEF12"))
    store.set_step_state(run.run_id, "notify", 1, StepState.DISPATCHED, detail=f"Authorization: Bearer {CANARY}")
    values = {"text": f"leaked {CANARY}", "token": "xoxb-1111111111-abcdefghij"}
    observation = Observation("posted", "capalis:slack.message.get", Target("slack.channel", "C0ABCDEF12"),
                              EvidenceLevel.V1, NOW, values, error=f"echo {CANARY}")
    store.record_observation(run.run_id, "notify", observation,
                             ContractVerdict("posted", ContractStatus.MET, EvidenceLevel.V1, f"saw {CANARY}"))
    store.append_event("note", {CANARY: CANARY, "list": [CANARY]}, run_id=run.run_id)
    store.conn.execute("PRAGMA wal_checkpoint(FULL)")

    raw = b"".join(p.read_bytes() for p in db_path.parent.glob(db_path.name + "*"))
    assert CANARY.encode() not in raw
    assert b"xoxb-1111111111" not in raw
    stored = store.observations(run.run_id)
    assert stored[0]["values"]["text"] == "leaked [redacted]"
    assert store.attempts(run.run_id)[0].detail == "Authorization: Bearer [redacted]"


def test_display_names_persist(open_store: Callable[[], Store]) -> None:
    store = open_store()
    store.set_names({"slack.channel:C0CFU6F0MNK": "#engineering"})
    store.set_names({"slack.channel:C0CFU6F0MNK": "#eng", "discord.user:1": "@ada"})
    assert open_store().names() == {"slack.channel:C0CFU6F0MNK": "#eng", "discord.user:1": "@ada"}
