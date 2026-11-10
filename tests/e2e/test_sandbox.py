"""Live run of the hero Recipe against the owner's sandbox accounts.

Opt-in: ``CAPALIS_E2E=1`` with ``CAPALIS_GITHUB_TOKEN`` and ``CAPALIS_SLACK_BOT_TOKEN`` in the
environment. It creates one real issue and one real Slack message per run, both labeled as tests.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from capalis.adapters import inventory
from capalis.adapters.catalog import GITHUB_TOKEN, SLACK_BOT_TOKEN
from capalis.core.compiler import compile_recipe
from capalis.core.ids import idempotency_key
from capalis.core.lifecycle import RecipeState, RunState, StepState
from capalis.core.model import grant_for
from capalis.core.policy import Mode
from capalis.core.redact import Redactor
from capalis.runtime.ports import Invocation, Outcome
from capalis.runtime.runner import Runner
from capalis.storage.db import Store
from support import DISCORD, REPO, REPORTER, SLACK, hero_draft

SANDBOX_REPO = os.environ.get("CAPALIS_E2E_REPO", "oluwafemi-akolade/capalis-sandbox")
SANDBOX_CHANNEL = os.environ.get("CAPALIS_E2E_SLACK_CHANNEL", "C0CFU6F0MNK")
SANDBOX_DISCORD = os.environ.get("CAPALIS_E2E_DISCORD_CHANNEL", "1569488960479297590")
SANDBOX_REPORTER = os.environ.get("CAPALIS_E2E_REPORTER", "1145236794108035144")
EVIDENCE = Path(__file__).resolve().parents[2] / ".dev" / "evidence"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("CAPALIS_E2E") != "1" or not os.environ.get(GITHUB_TOKEN)
                       or not os.environ.get(SLACK_BOT_TOKEN), reason="live sandbox run is opt-in"),
]


def _sandbox_draft() -> dict[str, Any]:
    text = json.dumps(hero_draft())
    for old, new in ((REPO, SANDBOX_REPO), (SLACK, SANDBOX_CHANNEL), (DISCORD, SANDBOX_DISCORD),
                     (REPORTER, SANDBOX_REPORTER)):
        text = text.replace(old, new)
    draft: dict[str, Any] = json.loads(text)
    draft["steps"][1]["inputs"]["title"] = {"template": "[Capalis e2e] Bug report from {trigger.author}"}
    return draft


def test_hero_recipe_live_on_sandbox(tmp_path: Path) -> None:
    adapters = inventory.build(secret=os.environ.get)
    result = compile_recipe(_sandbox_draft(), inventory.descriptors(adapters), recipe_id="bug-reports-e2e",
                            version=1, owner="owner", workspace="sandbox")
    assert result.ok and result.recipe is not None, result.problems
    recipe = result.recipe
    conn = sqlite3.connect(tmp_path / "e2e.db", check_same_thread=False)
    store = Store(conn, redactor=Redactor([os.environ[GITHUB_TOKEN], os.environ[SLACK_BOT_TOKEN]]))
    store.save_recipe(recipe)
    for state in (RecipeState.TESTED, RecipeState.SHADOW_QUALIFIED, RecipeState.AWAITING_APPROVAL):
        store.transition(recipe.digest, state)
    runner = Runner(store, adapters)

    message_id = f"e2e-{uuid.uuid4().hex[:12]}"
    payload = {"message_id": message_id, "author": "capalis-e2e",
               "text": "[Capalis e2e] Checkout button does nothing on Safari"}
    source = f"discord:channel:{SANDBOX_DISCORD}"

    shadow = runner.submit(recipe, source=source, sender=SANDBOX_REPORTER, payload=payload, mode=Mode.SHADOW)
    assert shadow.state is RunState.UNVERIFIED, shadow
    assert store.latest_attempts(shadow.run_id)["create"].state is StepState.SIMULATED

    store.save_grant(grant_for(recipe, grant_id="e2e", actor="owner", now=int(time.time())))
    store.transition(recipe.digest, RecipeState.LIVE)
    live = runner.submit(recipe, source=source, sender=SANDBOX_REPORTER, payload=payload)
    attempts = store.latest_attempts(live.run_id)
    assert live.state is RunState.VERIFIED, (live, {k: (a.state, a.detail) for k, a in attempts.items()})

    duplicate = runner.submit(recipe, source=source, sender=SANDBOX_REPORTER, payload=payload)
    assert duplicate.duplicate and duplicate.run_id == live.run_id

    issue, message = attempts["create"].output, attempts["notify"].output
    # Reconciliation depends on these lookups finding the effects by key on the real services.
    for cid, target_param, target, expected in (
            ("capalis:github.issue.find_by_key", "repo", SANDBOX_REPO, issue["number"]),
            ("capalis:slack.message.find_by_key", "channel", SANDBOX_CHANNEL, message["ts"])):
        key = idempotency_key(recipe.recipe_id, message_id, "create" if "github" in cid else "notify")
        lookup = Invocation(cid, None, {target_param: target, "idempotency_key": key,
                                        "since": int(time.time()) - 600}, None, 30)
        # Lists can lag behind writes; the runner only trusts "not found" after the same settle window.
        deadline = time.time() + 60
        while True:
            found = adapters[cid].invoke(lookup)
            if (found.output or {}).get("found") or time.time() > deadline:
                break
            time.sleep(3)
        assert found.outcome is Outcome.OK and found.output is not None and found.output["found"], (cid, found)
        resource = found.output["resource"]
        assert resource.get("number", resource.get("ts")) == expected

    assert store.verify_chain() == (True, None)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    record = {"at": int(time.time()), "recipe_digest": recipe.digest, "run_id": live.run_id,
              "state": live.state.value, "issue_url": issue["html_url"], "slack_ts": message["ts"],
              "observations": store.observations(live.run_id),
              "events": [{"seq": e.seq, "kind": e.kind, "payload": e.payload} for e in store.events(live.run_id)]}
    (EVIDENCE / f"e2e-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(record, indent=2))
    conn.close()
