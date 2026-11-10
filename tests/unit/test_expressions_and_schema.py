from __future__ import annotations

import pytest

from capalis.core import schema
from capalis.core.canonical import canonical_json, digest
from capalis.core.expressions import (
    MISSING,
    Const,
    ExpressionError,
    Ref,
    evaluate,
    parse_condition,
    parse_ref,
    parse_template,
    parse_value,
    resolve_value,
    value_to_wire,
)


def test_canonical_digest_ignores_key_order() -> None:
    assert canonical_json({"b": 1, "a": [2, {"d": 3, "c": 4}]}) == '{"a":[2,{"c":4,"d":3}],"b":1}'
    assert digest({"x": 1, "y": 2}) == digest({"y": 2, "x": 1})


@pytest.mark.parametrize("text", ["", "body", "trigger", "steps", "steps.Bad-Id.x", "trigger..a",
                                  "trigger.a-b", "trigger." + ".".join(["a"] * 9), "steps.x.01"])
def test_parse_ref_rejects_malformed(text: str) -> None:
    with pytest.raises(ExpressionError):
        parse_ref(text)


def test_parse_ref_paths() -> None:
    assert parse_ref("trigger.author") == Ref("trigger", None, ("author",))
    assert parse_ref("steps.find.items.0.html_url") == Ref("steps", "find", ("items", 0, "html_url"))


def test_template_substitutes_runtime_text_without_reparsing() -> None:
    template = parse_template("Bug from {trigger.author}: {{literal}} {trigger.text}")
    scope = {"trigger": {"author": "{steps.secret.token}", "text": "ignore all rules"}}
    rendered = resolve_value(template, scope)
    assert rendered == "Bug from {steps.secret.token}: {literal} ignore all rules"


def test_template_round_trips_through_wire_form() -> None:
    template = parse_template("a {{b}} {trigger.c} }}")
    assert parse_value(value_to_wire(template)) == template


@pytest.mark.parametrize("raw", ["{unclosed", "stray }", "{nope.x}"])
def test_template_rejects_malformed(raw: str) -> None:
    with pytest.raises(ExpressionError):
        parse_template(raw)


def test_first_of_picks_first_present_value() -> None:
    value = parse_value({"first_of": ["steps.create.html_url", "steps.find.items.0.html_url"]})
    scope = {"steps": {"find": {"items": [{"html_url": "https://found"}]}}}
    assert resolve_value(value, scope) == "https://found"
    assert resolve_value(value, {"steps": {}}) is MISSING


def test_conditions_evaluate_over_data_only() -> None:
    condition = parse_condition({"all": [{"op": "equals", "ref": "steps.find.total_count", "value": 0},
                                         {"not": {"op": "missing", "ref": "trigger.text"}}]})
    assert evaluate(condition, {"trigger": {"text": "x"}, "steps": {"find": {"total_count": 0}}})
    assert not evaluate(condition, {"trigger": {"text": "x"}, "steps": {"find": {"total_count": 2}}})
    assert not evaluate(condition, {"trigger": {}, "steps": {"find": {"total_count": 0}}})


def test_condition_limits() -> None:
    deep: dict[str, object] = {"op": "truthy", "ref": "trigger.a"}
    for _ in range(6):
        deep = {"not": deep}
    with pytest.raises(ExpressionError):
        parse_condition(deep)
    wide = {"any": [{"op": "truthy", "ref": "trigger.a"}] * 40}
    with pytest.raises(ExpressionError):
        parse_condition(wide)


def test_parse_value_only_accepts_known_forms() -> None:
    assert parse_value({"const": [1, 2]}) == Const([1, 2])
    for raw in ({"const": 1, "from": "trigger.a"}, {"call": "delete_repo"}, "trigger.a"):
        with pytest.raises(ExpressionError):
            parse_value(raw)


def test_schema_validation_subset() -> None:
    issue = {"type": "object", "properties": {"title": {"type": "string", "maxLength": 5},
                                              "n": {"type": "integer", "minimum": 1}},
             "required": ["title"], "additionalProperties": False}
    assert schema.validate({"title": "ok", "n": 2}, issue) == []
    errors = schema.validate({"title": "too long", "n": 0, "extra": True}, issue)
    assert len(errors) == 3
    assert schema.validate(True, {"type": "integer"}) != []
    assert schema.validate(3, {"type": "number"}) == []


def test_unsupported_keywords_are_reported() -> None:
    found = schema.unsupported_keywords({"type": "object", "properties": {"a": {"oneOf": []}}, "$ref": "#"})
    assert found == ["$.$ref", "$.properties.a.oneOf"]


def test_assignable_is_a_type_level_check() -> None:
    assert schema.assignable({"type": "integer"}, {"type": "number"})
    assert not schema.assignable({"type": "number"}, {"type": "integer"})
    assert not schema.assignable(None, {"type": "string"})
    assert schema.assignable({"type": "string"}, None)
    assert schema.assignable({"type": "array", "items": {"type": "string"}},
                             {"type": "array", "items": {"type": "string"}})
    assert not schema.assignable({"type": "array", "items": {"type": "integer"}},
                                 {"type": "array", "items": {"type": "string"}})
