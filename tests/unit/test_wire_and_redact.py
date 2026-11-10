from __future__ import annotations

import copy
from typing import Any

import pytest

from capalis.core.ids import idempotency_key, sender_fingerprint
from capalis.core.model import RecipeSpec, grant_for
from capalis.core.redact import REDACTED, Redactor
from capalis.core.wire import WireError, grant_from_wire, recipe_from_wire
from support import compile_draft, hero_draft, manual_trigger, with_deploy_wait


def _compiled(draft: dict[str, Any]) -> RecipeSpec:
    result = compile_draft(draft)
    assert result.ok and result.recipe is not None, result.problems
    return result.recipe


def _rich_draft() -> dict[str, Any]:
    draft = with_deploy_wait(hero_draft())
    draft["steps"][1]["verify"].append({
        "id": "site_up", "probe": "capalis:http.get", "level": "V2", "deadline_s": 3600,
        "inputs": {"origin": {"const": "https://shop.example.com"}, "path": {"const": "/status"}},
        "expect": [{"path": "status", "op": "equals", "value": {"const": 200}}],
    })
    draft["steps"][2]["inputs"]["text"] = {"first_of": ["steps.create.html_url", "steps.find.items.0.html_url"]}
    return draft


@pytest.mark.parametrize("draft", [
    hero_draft(),
    _rich_draft(),
    manual_trigger(hero_draft()),
    manual_trigger(hero_draft(), kind="schedule", schedule="0 9 * * 1-5"),
], ids=["hero", "wait-v2-first_of", "manual", "schedule"])
def test_recipe_round_trips_through_its_wire_form(draft: dict[str, Any]) -> None:
    recipe = _compiled(copy.deepcopy(draft))
    restored = recipe_from_wire(recipe.to_wire(), expected_digest=recipe.digest)
    assert restored == recipe
    assert restored.digest == recipe.digest
    assert restored.to_wire() == recipe.to_wire()


def test_tampered_wire_fails_the_digest_check() -> None:
    recipe = _compiled(hero_draft())
    wire = recipe.to_wire()
    wire["steps"][1]["target"]["value"] = "attacker/repo"
    with pytest.raises(WireError, match="digest"):
        recipe_from_wire(wire, expected_digest=recipe.digest)


@pytest.mark.parametrize("mutate", [
    lambda w: w.pop("steps"),
    lambda w: w["steps"][0].update(effect="teleport"),
    lambda w: w["trigger"].update(kind="telepathy"),
    lambda w: w["steps"][2]["inputs"].update(text={"from": "nowhere.at.all"}),
    lambda w: w.update(budgets={"surprise": 1}),
])
def test_malformed_wire_raises_wire_error(mutate: Any) -> None:
    wire = _compiled(hero_draft()).to_wire()
    mutate(wire)
    with pytest.raises(WireError):
        recipe_from_wire(wire)


def test_grant_round_trips() -> None:
    recipe = _compiled(hero_draft())
    grant = grant_for(recipe, grant_id="g-1", actor="owner", now=1_000, ttl_s=60)
    assert grant_from_wire(grant.to_wire()) == grant
    with pytest.raises(WireError):
        grant_from_wire({**grant.to_wire(), "effects": ["teleport"]})


def test_idempotency_key_is_stable_and_scoped() -> None:
    key = idempotency_key("bug-reports", "msg-1", "create")
    assert key == idempotency_key("bug-reports", "msg-1", "create")
    assert key.startswith("cap-") and len(key) == 36
    assert len({key, idempotency_key("bug-reports", "msg-2", "create"),
                idempotency_key("bug-reports", "msg-1", "notify"), idempotency_key("other", "msg-1", "create")}) == 4
    assert sender_fingerprint("discord", "123") != sender_fingerprint("slack", "123")


CANARY = "canary-7f3a9c1e5b2d4086"


@pytest.mark.parametrize("token", [
    "github_pat_" + "11ABCDEFG0123456789_" + "abcdefghijklmnopqrstuvwxyz",
    "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789",
    "xoxb-" + "1234567890-0987654321-abcdefghijklmnop",
    "xapp-" + "1-A0123456789-0123456789-abcdef",
    "https://hooks.slack.com/services/" + "T000/B000/XXXXXXXXXXXXXXXX",
    "123456789:AAH-abcdefghijklmnopqrstuvwxyz012345",
    "sk-proj-abcdefghijklmnopqrstuvwxyz",
    "Bearer abcdefghijklmnopqrstuvwxyz.123",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
])
def test_credential_formats_are_masked(token: str) -> None:
    out = Redactor().text(f"before {token} after")
    assert token not in out and REDACTED in out
    assert out.startswith("before ") and out.endswith(" after")


def test_known_secrets_are_masked_recursively_including_keys() -> None:
    redactor = Redactor([CANARY, "short"])
    payload = {"body": f"token={CANARY}", "nested": [{"x": CANARY}, (CANARY, 3)], CANARY: True, "n": 7}
    out = redactor.value(payload)
    assert CANARY not in repr(out)
    assert out["n"] == 7 and out["nested"][1][1] == 3
    # Values below the minimum length would mask ordinary words, so they are not treated as secrets.
    assert redactor.text("a short note") == "a short note"


def test_longer_secret_masked_whole_when_it_contains_a_shorter_one() -> None:
    inner = "abcdefgh12345678"
    outer = inner + "-suffix-99"
    assert Redactor([inner, outer]).text(outer) == REDACTED
